"""The whole chain, with nothing stubbed: acquisition, transcription, clip
intelligence, rendering.

Every other test file exercises one layer against fixtures. This one runs a
real media file through all four and asserts that each stage consumed what the
one before it produced — which is the failure mode integration tests exist for,
and the one unit tests structurally cannot see. Two layers can each be correct
and still disagree about pixel dimensions, millisecond units, or where a file
lives, and the disagreement only surfaces here.

What is real:

* the media — espeak-ng speaks, ffmpeg muxes, so there is an actual waveform
* acquisition — copied into the workspace, probed by parsing the MP4 boxes,
  persisted, with the duration read from the file rather than declared
* transcription — audio extracted by ffmpeg, decoded by a real recogniser
  running locally, with real word timings
* rendering — ffmpeg executes the composed filtergraph and produces a file,
  which is then measured

What is not Whisper: the recogniser here is pocketsphinx, because the Whisper
weights cannot be fetched in this environment. It is a real speech recogniser
and its timings are real, but its accuracy is poor. So this file asserts on
*plumbing* — units, ordering, dimensions, persistence, whether the next stage
accepted the input — and never on which words came back. An assertion on
transcript content would be an assertion about pocketsphinx, and it would fail
the day the provider is swapped for the one that belongs there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import timedelta

from clipforge.acquire import AcquisitionConfig, AcquisitionEngine
from clipforge.factory.channel import Channel
from clipforge.factory.niches import Niche, profile
from clipforge.factory.pipeline import Pipeline, PipelineConfig, Stage
from clipforge.factory.sources import Rights, RightsBasis, Source, SourceKind
from clipforge.gameplay.types import GameplayAsset
from clipforge.publish.types import utcnow
from clipforge.render import RenderConfig, RenderEngine, RenderRequest
from clipforge.store import MemoryDatabase, TenantRecord
from clipforge.transcribe import (
    AudioConfig,
    EngineTranscriber,
    SphinxProvider,
    TranscriptionConfig,
    TranscriptionEngine,
    TranscriptionState,
)

TENANT = "ten_e2e"

FFMPEG = os.environ.get("CLIPFORGE_FFMPEG") or shutil.which("ffmpeg") or ""
ESPEAK = shutil.which("espeak-ng") or shutil.which("espeak") or ""

#: Long enough to clear acquisition's 30-second floor and to give the moment
#: detector more than one window to choose between. Business vocabulary,
#: because the channel below is a business channel and the niche scorer reads
#: domain terms.
SCRIPT = (
    "The raise was the mistake. We went from twelve people to ninety in "
    "seven months and we almost went bankrupt doing it. "
    "We burned fourteen million dollars in nineteen months. "
    "Nobody tells you that headcount is not progress. "
    "I confused the two for two years and it nearly killed the company. "
    "The revenue never followed the hiring. It never does. "
    "We cut the team in half and the product shipped faster than before. "
    "That is the part nobody writes about in the funding announcement. "
    "Growth is not the same thing as a business. "
    "The second time around we hired nobody for a year and we were profitable."
)


def _sphinx_available() -> bool:
    try:
        import pocketsphinx  # noqa: F401
    except Exception:                                   # noqa: BLE001
        return False
    return True


@unittest.skipUnless(
    FFMPEG and ESPEAK and _sphinx_available(),
    "needs ffmpeg, espeak-ng and pocketsphinx",
)
class EndToEndTest(unittest.TestCase):
    """One source, four layers, one rendered file."""

    media: str = ""
    directory: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.mkdtemp(prefix="clipforge-e2e-")
        raw = os.path.join(cls.directory, "speech.wav")
        subprocess.run([ESPEAK, "-s", "125", "-w", raw, SCRIPT],
                       check=True, capture_output=True)
        cls.media = os.path.join(cls.directory, "the-raise-was-a-mistake.mp4")
        # 1920x1080: the composer builds its camera window against a
        # `SpeakerTrack` with no samples, which assumes a 1080p source. A 360p
        # fixture would make the renderer refuse a crop larger than the frame
        # — correctly — and the refusal would be about the fixture, not the
        # code under test.
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
             "-i", raw,
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", cls.media],
            check=True, capture_output=True,
        )
        cls.gameplay = os.path.join(cls.directory, "bed.mp4")
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "smptebars=size=1080x1920:rate=30:duration=60",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             cls.gameplay],
            check=True, capture_output=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.directory, ignore_errors=True)

    def setUp(self) -> None:
        self.workspace = tempfile.mkdtemp(prefix="clipforge-e2ew-")
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.db = MemoryDatabase()
        with self.db.unit_of_work(TENANT) as uow:
            uow.tenants.save(TenantRecord(id=TENANT, name="End to end"))

    # -- the chain ---------------------------------------------------------

    def _acquire(self) -> str:
        engine = AcquisitionEngine(
            self.db, TENANT,
            config=AcquisitionConfig(workspace=self.workspace),
        )
        jobs = engine.submit(self.media)
        self.assertEqual(len(jobs), 1)
        results = engine.run(limit=1)
        self.assertEqual(len(results), 1, "acquisition produced no result")

        with self.db.unit_of_work(TENANT) as uow:
            sources = uow.sources.all()
        self.assertEqual(len(sources), 1)
        return sources[0].id

    def _transcription_engine(self) -> TranscriptionEngine:
        return TranscriptionEngine(
            self.db, TENANT, SphinxProvider(),
            config=TranscriptionConfig(
                workspace=os.path.join(self.workspace, "transcribe"),
                audio=AudioConfig(ffmpeg=FFMPEG),
            ),
        )

    def _channel(self) -> Channel:
        # Accounts, because the last stage builds one post spec per connected
        # platform and a channel with none is correctly blocked before it.
        # Nothing is published here — the publisher is not in this chain.
        return Channel(
            channel_id="ch_e2e",
            name=profile(Niche.BUSINESS).label,
            niche=Niche.BUSINESS,
            topics=("business", "startups"),
            accounts={p: f"{p.value}-e2e" for p in profile(Niche.BUSINESS).platforms},
        )

    def _source(self, source_id: str, duration_s: float) -> Source:
        return Source(
            source_id, "The raise was a mistake",
            kind=SourceKind.PODCAST,
            rights=Rights(basis=RightsBasis.OWNED),
            creator="Founder", duration_s=duration_s,
            topics=("business", "startups"), language="en",
            has_transcript=True, published_at=utcnow() - timedelta(days=2),
        )

    # -- tests -------------------------------------------------------------

    def test_acquisition_measures_the_file_rather_than_trusting_it(self) -> None:
        source_id = self._acquire()
        with self.db.unit_of_work(TENANT) as uow:
            source = uow.sources.get(source_id)
            runs = uow.acquisitions.for_source(source_id)

        self.assertGreater(source.duration_s, 30.0,
                           "duration was not read off the file")
        self.assertFalse(source.has_transcript)
        self.assertTrue(runs, "the acquisition left no run record")
        self.assertTrue(os.path.exists(runs[0].media_path),
                        "the acquired media is not where the run says it is")

    def test_transcription_consumes_what_acquisition_produced(self) -> None:
        """The handover: transcription is given a source id and nothing else,
        and has to find the media the acquisition layer put somewhere."""

        source_id = self._acquire()
        engine = self._transcription_engine()
        transcriber = EngineTranscriber(engine)

        found = transcriber.media_for(self._source(source_id, 40.0))
        self.assertTrue(found and os.path.exists(found),
                        "transcription could not find the acquired media")

        words = transcriber.transcribe(self._source(source_id, 40.0))
        self.assertTrue(words, "no words came back")
        for word in words:
            self.assertGreater(word.end_ms, word.start_ms)

        with self.db.unit_of_work(TENANT) as uow:
            run = uow.transcriptions.for_source(source_id)
            source = uow.sources.get(source_id)
        self.assertEqual(run.state, TranscriptionState.SUCCEEDED.value)
        self.assertGreater(run.word_count, 0)
        self.assertTrue(source.has_transcript,
                        "the library still says this source has no transcript")

    def test_the_full_chain_produces_a_rendered_file(self) -> None:
        """Acquisition to a playable 1080x1920 clip, one stage at a time.

        The assertions are about handover, not quality: that the detector was
        given real timings, that the composer sized a plan against them, and
        that ffmpeg executed the result and produced a file with the vertical
        geometry every one of these platforms requires.
        """

        source_id = self._acquire()
        with self.db.unit_of_work(TENANT) as uow:
            duration_s = uow.sources.get(source_id).duration_s

        transcriber = EngineTranscriber(self._transcription_engine())
        pipeline = Pipeline(PipelineConfig(
            transcriber=transcriber,
            # The bed the Business profile asks for, not one chosen here: the
            # composer refuses a library that does not hold its niche's game.
            gameplay_library=(
                GameplayAsset("bed", profile(Niche.BUSINESS).gameplay_bed,
                              60.0, 1080, 1920, 30.0),
            ),
        ))

        item = pipeline.run(self._channel(), self._source(source_id, duration_s))
        self.assertNotIn(item.stage, (Stage.BLOCKED, Stage.FAILED),
                         f"pipeline stopped at {item.stage.value}: {item.reason}")
        self.assertTrue(item.words, "the pipeline reached detection with no words")
        self.assertIsNotNone(item.moment, "no moment was chosen")
        self.assertIsNotNone(item.gameplay_plan, "nothing was composed")
        self.assertIsNotNone(item.caption_track, "no captions were built")

        plan = item.gameplay_plan
        self.assertEqual((plan.width, plan.height), (1080, 1920))

        # The captions came from the transcript, so their timings have to sit
        # inside the media. A caption at 400s over a 40s clip means somebody
        # mixed up seconds and milliseconds between two layers.
        last = max(cue.end_ms for cue in item.caption_track.cues)
        self.assertLessEqual(last / 1000.0, duration_s + 1.0,
                             "captions run past the end of the source")

        renderer = RenderEngine(
            self.db, TENANT,
            config=RenderConfig(workspace=self.workspace, ffmpeg=FFMPEG,
                                preset="ultrafast"),
        )
        output = os.path.join(self.workspace, "clip.mp4")
        result = renderer.render(RenderRequest(
            render_id="rnd_e2e", plan=plan,
            speaker_path=self.media, gameplay_path=self.gameplay,
            output_path=output, clip_id="cl_e2e", source_id=source_id,
            start_s=item.moment.candidate.start_ms / 1000.0,
        ))

        self.assertTrue(os.path.exists(result.output_path))
        self.assertGreater(result.size_bytes, 0)
        self.assertEqual((result.probe.width, result.probe.height), (1080, 1920))
        self.assertTrue(result.probe.has_audio,
                        "the rendered clip has no audio — the speech was lost")
        self.assertAlmostEqual(result.probe.duration_s, plan.duration_s, delta=1.5)


if __name__ == "__main__":
    unittest.main()
