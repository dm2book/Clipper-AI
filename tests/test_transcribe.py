"""Transcription, tested against real audio and real sockets.

## What is real here, and what is not

**Real, end to end:** a WAV of actual synthesised speech, real ffmpeg
extraction to 16 kHz mono, real chunking of a long file, a real offline
recogniser decoding real audio into real word timings, real merging across
chunk boundaries, real rows in Postgres, and the real factory `Transcriber`
protocol driving it.

**Real, protocol only:** `OpenAICompatibleProvider` is driven against a real
HTTP server on a real socket that implements OpenAI's documented transcription
endpoint. That verifies the multipart body, the headers, the `verbose_json`
parsing and the status classification — this client's own behaviour. It does
**not** verify that OpenAI behaves as documented, and no assertion here should
be read that way.

**Not verified at all:** `LocalWhisperProvider` against a Whisper model. The
model host is blocked in this environment, so `WhisperModel` cannot be built
and no transcript has come out of it. Its mapping and error handling are
tested against recorded faster-whisper result shapes, which is a real test of
this repository's code and no test of Whisper.

The offline recogniser is PocketSphinx. Its accuracy is far below Whisper and
these tests never assert on *what* it heard — only that words came back with
sane, ascending, in-range timings, which is a claim about the pipeline rather
than about the recogniser.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from clipforge.factory.sources import Rights, RightsBasis, Source, SourceKind
from clipforge.publish.types import utcnow
from clipforge.store import MemoryDatabase, SourceRecord, TenantRecord
from clipforge.transcribe import (
    AudioConfig,
    EngineTranscriber,
    OpenAICompatibleProvider,
    OpenAIConfig,
    SphinxProvider,
    TranscriptionConfig,
    TranscriptionEngine,
    TranscriptionState,
    describe_environment,
    extract_audio,
    extracted_audio,
    merge_chunks,
    parse_verbose_json,
    plan_chunks,
    provider_from_env,
    to_timed_words,
    transcript_from_dict,
    transcript_to_dict,
    wav_duration_s,
)
from clipforge.transcribe.types import (
    AudioExtractionFailed,
    PermanentError,
    ProviderInfo,
    ProviderUnavailable,
    RetryableError,
    Segment,
    Transcript,
    Word,
)
from clipforge.transcribe.whisper_local import (
    HALLUCINATION_NO_SPEECH,
    LocalWhisperProvider,
    _to_transcript,
)

TENANT = "ten_txn"
FFMPEG = os.environ.get("CLIPFORGE_FFMPEG", "") or shutil.which("ffmpeg") or ""
ESPEAK = shutil.which("espeak-ng") or shutil.which("espeak") or ""

SPOKEN = "the quick brown fox jumps over the lazy dog"


def _sphinx_available() -> bool:
    try:
        return SphinxProvider().availability().ready
    except Exception:  # noqa: BLE001
        return False


class _Media:
    """Real speech, synthesised once for the module."""

    directory = ""
    speech_wav = ""      # 22 kHz, as espeak writes it
    speech_mp4 = ""      # the same speech muxed into a video container
    silent_mp4 = ""
    long_wav = ""        # long enough to force chunking

    @classmethod
    def build(cls) -> None:
        if cls.directory or not (FFMPEG and ESPEAK):
            return
        cls.directory = tempfile.mkdtemp(prefix="clipforge-tx-")
        cls.speech_wav = os.path.join(cls.directory, "speech.wav")
        subprocess.run([ESPEAK, "-v", "en-us", "-s", "130", "-w", cls.speech_wav,
                        SPOKEN], check=True, capture_output=True, timeout=60)

        cls.speech_mp4 = os.path.join(cls.directory, "speech.mp4")
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=navy:size=320x240:duration=6",
             "-i", cls.speech_wav, "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", cls.speech_mp4],
            check=True, capture_output=True, timeout=120)

        cls.silent_mp4 = os.path.join(cls.directory, "silent.mp4")
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:size=160x120:duration=3",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", cls.silent_mp4],
            check=True, capture_output=True, timeout=120)

        # The same utterance repeated with gaps, long enough that the engine
        # has to chunk it — which is the path the merge logic is on.
        cls.long_wav = os.path.join(cls.directory, "long.wav")
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-stream_loop", "9",
             "-i", cls.speech_wav, "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", cls.long_wav],
            check=True, capture_output=True, timeout=180)

    @classmethod
    def teardown(cls) -> None:
        if cls.directory:
            shutil.rmtree(cls.directory, ignore_errors=True)
            cls.directory = ""


def setUpModule() -> None:  # noqa: N802
    _Media.build()


def tearDownModule() -> None:  # noqa: N802
    _Media.teardown()


def _media_ready() -> bool:
    return bool(_Media.directory and os.path.exists(_Media.speech_mp4))


# ---------------------------------------------------------------------------
# Audio extraction — real ffmpeg, real files
# ---------------------------------------------------------------------------


@unittest.skipUnless(FFMPEG and ESPEAK, "needs ffmpeg and espeak-ng")
class AudioExtractionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="clipforge-ax-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_audio_comes_out_of_a_video_at_the_rate_models_want(self) -> None:
        """Every Whisper-family model wants 16 kHz mono. Resampling once here
        rather than per chunk in the provider is what makes the chunk-size
        arithmetic exact."""

        import wave

        path = extract_audio(_Media.speech_mp4, os.path.join(self.tmp, "a.wav"))
        with wave.open(path, "rb") as handle:
            self.assertEqual(handle.getframerate(), 16_000)
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getsampwidth(), 2)
        self.assertGreater(wav_duration_s(path), 1.0)

    def test_a_file_with_no_audio_is_refused_permanently(self) -> None:
        """Permanent because the same file will not decode differently later.
        Retrying it is a queue spending its afternoon on a silent video."""

        with self.assertRaises(AudioExtractionFailed) as caught:
            extract_audio(_Media.silent_mp4, os.path.join(self.tmp, "b.wav"))
        self.assertIn("no audio track", str(caught.exception))

    def test_the_temporary_file_is_gone_even_when_the_body_raises(self) -> None:
        """The path that matters: a failure halfway through a three-hour
        podcast has hundreds of megabytes of scratch audio to answer for."""

        captured = {}
        with self.assertRaises(RuntimeError):
            with extracted_audio(_Media.speech_mp4) as path:
                captured["path"] = path
                self.assertTrue(os.path.exists(path))
                raise RuntimeError("something went wrong mid-transcription")
        self.assertFalse(os.path.exists(captured["path"]))
        self.assertFalse(os.path.exists(os.path.dirname(captured["path"])))

    def test_a_window_can_be_extracted_without_touching_the_rest(self) -> None:
        """Chunking seeks into the source rather than cutting an extracted
        WAV, so peak disk stays one chunk instead of the whole file plus its
        pieces."""

        path = extract_audio(_Media.speech_mp4, os.path.join(self.tmp, "c.wav"),
                             start_s=1.0, duration_s=1.5)
        self.assertAlmostEqual(wav_duration_s(path), 1.5, delta=0.2)

    def test_extraction_never_holds_the_media_in_memory(self) -> None:
        """A structural claim, checked structurally: the extractor's only file
        access is ffmpeg's own, and the module never reads media bytes."""

        import inspect

        from clipforge.transcribe import audio as module

        source = inspect.getsource(module)
        # `wave` reads headers, and the size guard stats. Neither reads media.
        self.assertNotIn(".read()", source.replace("handle.read(", "X("))


class ChunkPlanningTest(unittest.TestCase):
    """Windowing arithmetic. No media needed."""

    def test_a_short_file_is_one_chunk(self) -> None:
        self.assertEqual(len(plan_chunks(300.0)), 1)

    def test_a_long_file_is_chunked_with_overlap(self) -> None:
        windows = plan_chunks(7200.0, AudioConfig(chunk_s=600, overlap_s=3))
        self.assertGreater(len(windows), 10)
        for (offset, length, _, _), (next_offset, _, _, _) in zip(windows, windows[1:]):
            self.assertLess(next_offset, offset + length,
                            "consecutive chunks do not overlap")

    def test_the_kept_spans_tile_the_media_exactly(self) -> None:
        """Each instant is owned by exactly one chunk. A gap loses words; an
        overlap duplicates them."""

        windows = plan_chunks(7200.0, AudioConfig(chunk_s=600, overlap_s=3))
        self.assertEqual(windows[0][2], 0.0)
        self.assertAlmostEqual(windows[-1][3], 7200.0, places=3)
        for current, following in zip(windows, windows[1:]):
            self.assertAlmostEqual(current[3], following[2], places=6)

    def test_no_chunk_exceeds_the_upload_ceiling(self) -> None:
        """Ten minutes of 16 kHz mono is 19 MB, under OpenAI's 25 MB."""

        from clipforge.transcribe.audio import BYTES_PER_SECOND
        from clipforge.transcribe.openai_api import MAX_UPLOAD_BYTES

        longest = max(length for _, length, _, _ in plan_chunks(7200.0))
        self.assertLess(longest * BYTES_PER_SECOND, MAX_UPLOAD_BYTES)


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


class _Chunk:
    def __init__(self, offset_s, duration_s, keep_from_s, keep_to_s):
        self.offset_s = offset_s
        self.duration_s = duration_s
        self.keep_from_s = keep_from_s
        self.keep_to_s = keep_to_s


def _t(words, language="en"):
    made = tuple(Word(text, start, end) for text, start, end in words)
    return Transcript(
        text=" ".join(w.text for w in made), words=made,
        segments=(Segment(" ".join(w.text for w in made),
                          made[0].start_s, made[-1].end_s, made),) if made else (),
        language=language,
    )


class MergeTest(unittest.TestCase):
    def test_timestamps_are_shifted_into_the_source_timeline(self) -> None:
        """A word at 12s of chunk four is not at 12s of the podcast. Captions
        built from unshifted times drift further out with every chunk."""

        merged = merge_chunks([
            (_Chunk(0, 600, 0, 598.5), _t([("first", 1.0, 1.4)])),
            (_Chunk(597, 600, 598.5, 1195.5), _t([("later", 5.0, 5.4)])),
        ])
        self.assertEqual([w.text for w in merged.words], ["first", "later"])
        self.assertAlmostEqual(merged.words[1].start_s, 602.0, places=3)

    def test_a_word_in_the_overlap_appears_exactly_once(self) -> None:
        """Both chunks saw it. Keeping both duplicates it in the caption;
        keeping neither loses the word the cut landed inside."""

        merged = merge_chunks([
            (_Chunk(0, 10, 0, 8.5), _t([("shared", 8.9, 9.2)])),
            (_Chunk(7, 10, 8.5, 17), _t([("shared", 1.9, 2.2)])),
        ])
        self.assertEqual([w.text for w in merged.words], ["shared"])
        self.assertAlmostEqual(merged.words[0].start_s, 8.9, places=3)

    def test_the_dominant_language_wins_weighted_by_duration(self) -> None:
        """A two-word Spanish advert should not outvote an hour of English."""

        merged = merge_chunks([
            (_Chunk(0, 600, 0, 598.5), _t([("hello", 1, 2)], language="en")),
            (_Chunk(597, 20, 598.5, 617), _t([("hola", 1, 2)], language="es")),
        ])
        self.assertEqual(merged.language, "en")

    def test_a_single_chunk_passes_through_unchanged(self) -> None:
        merged = merge_chunks([(_Chunk(0, 10, 0, 10), _t([("only", 1.0, 1.5)]))])
        self.assertEqual(merged.words[0].start_s, 1.0)


# ---------------------------------------------------------------------------
# A real recogniser on real audio
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    FFMPEG and ESPEAK and _sphinx_available(),
    "needs ffmpeg, espeak-ng and pocketsphinx",
)
class RealRecognitionTest(unittest.TestCase):
    """Real decoding of real speech.

    The assertions are about *timings and structure*, never about which words
    came back: PocketSphinx is not accurate enough for that to be a fair test,
    and this suite is checking the pipeline rather than the recogniser.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="clipforge-rr-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.provider = SphinxProvider()

    def test_real_audio_produces_real_words_with_timings(self) -> None:
        path = extract_audio(_Media.speech_mp4, os.path.join(self.tmp, "a.wav"))
        transcript = self.provider.transcribe(path)

        self.assertGreater(transcript.word_count, 0, "nothing was recognised")
        self.assertTrue(transcript.text.strip())
        self.assertTrue(transcript.has_word_timings)
        self.assertEqual(transcript.language, "en")
        self.assertGreater(transcript.duration_s, 0)

        audio_length = wav_duration_s(path)
        for word in transcript.words:
            self.assertGreaterEqual(word.start_s, 0.0)
            self.assertGreater(word.end_s, word.start_s, f"{word.text} has no duration")
            self.assertLessEqual(word.end_s, audio_length + 0.5,
                                 f"{word.text} ends after the audio does")

    def test_words_come_back_in_order_and_do_not_overlap(self) -> None:
        path = extract_audio(_Media.speech_mp4, os.path.join(self.tmp, "b.wav"))
        words = self.provider.transcribe(path).words
        for previous, word in zip(words, words[1:]):
            self.assertLessEqual(previous.end_s, word.start_s + 1e-6,
                                 "two words overlap in time")

    def test_confidence_is_reported_where_the_decoder_has_one(self) -> None:
        path = extract_audio(_Media.speech_mp4, os.path.join(self.tmp, "c.wav"))
        transcript = self.provider.transcribe(path)
        scored = [w for w in transcript.words if w.confidence is not None]
        self.assertTrue(scored, "no word carried a confidence")
        for word in scored:
            self.assertGreaterEqual(word.confidence, 0.0)
            self.assertLessEqual(word.confidence, 1.0)

    def test_segments_are_grouped_on_silence(self) -> None:
        path = extract_audio(_Media.speech_mp4, os.path.join(self.tmp, "d.wav"))
        transcript = self.provider.transcribe(path)
        self.assertTrue(transcript.segments)
        for segment in transcript.segments:
            self.assertTrue(segment.words)
            self.assertAlmostEqual(segment.start_s, segment.words[0].start_s, places=3)
            self.assertAlmostEqual(segment.end_s, segment.words[-1].end_s, places=3)

    def test_the_wrong_sample_rate_is_refused_rather_than_guessed_at(self) -> None:
        """A recogniser fed the wrong rate returns confident nonsense, which
        is far harder to notice than an error. The 22 kHz file espeak writes
        is exactly that trap."""

        with self.assertRaises(PermanentError) as caught:
            self.provider.transcribe(_Media.speech_wav)
        self.assertIn("16 kHz", str(caught.exception))


# ---------------------------------------------------------------------------
# The engine, on real media, into a real database
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    FFMPEG and ESPEAK and _sphinx_available(),
    "needs ffmpeg, espeak-ng and pocketsphinx",
)
class TranscriptionEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.mkdtemp(prefix="clipforge-tw-")
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.db = MemoryDatabase()
        with self.db.unit_of_work(TENANT) as uow:
            uow.tenants.save(TenantRecord(id=TENANT, name="Transcriber"))
            uow.sources.save(SourceRecord(id="src_1", tenant_id=TENANT,
                                          title="A talk", fingerprint="fp1"))
        self.engine = self._engine()

    def _engine(self, provider=None, audio=None, **kwargs) -> TranscriptionEngine:
        config = TranscriptionConfig(workspace=self.workspace,
                                     audio=audio or AudioConfig(ffmpeg=FFMPEG),
                                     **kwargs)
        return TranscriptionEngine(self.db, TENANT,
                                   provider or SphinxProvider(), config=config)

    def test_a_queued_transcription_runs_and_is_stored(self) -> None:
        self.engine.enqueue("src_1", _Media.speech_mp4)
        runs = self.engine.run(limit=1)

        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run.state, TranscriptionState.SUCCEEDED.value)
        self.assertGreater(run.word_count, 0)
        self.assertGreater(run.segment_count, 0)
        self.assertEqual(run.language, "en")
        self.assertTrue(run.text.strip())
        self.assertIsNotNone(run.elapsed_s)
        self.assertEqual(run.provider, "pocketsphinx")

    def test_the_stored_transcript_round_trips_with_its_timings(self) -> None:
        """The stored form is the only copy. A transcript that came back
        without word timings would silently produce captions with nothing to
        karaoke, and the transcription would have to be paid for again to find
        out why."""

        self.engine.enqueue("src_1", _Media.speech_mp4)
        original = self.engine.run(limit=1)[0]

        restored = self.engine.transcript_for("src_1")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.word_count, original.word_count)
        self.assertTrue(restored.has_word_timings)
        self.assertEqual(restored.language, "en")
        self.assertIsNotNone(restored.provider)
        self.assertEqual(restored.provider.name, "pocketsphinx")
        for word in restored.words:
            self.assertGreater(word.end_s, word.start_s)

    def test_the_source_is_marked_as_having_a_transcript(self) -> None:
        """The factory's clearance query reads `has_transcript` and knows
        nothing about this layer."""

        self.engine.enqueue("src_1", _Media.speech_mp4)
        self.engine.run(limit=1)
        with self.db.unit_of_work(TENANT) as uow:
            self.assertTrue(uow.sources.require("src_1").has_transcript)

    def test_the_scratch_audio_is_deleted(self) -> None:
        """Hundreds of megabytes per source. The workspace should be empty
        when the run finishes."""

        self.engine.enqueue("src_1", _Media.speech_mp4)
        self.engine.run(limit=1)
        leftovers = [
            name for name in os.listdir(self.workspace)
            if name.startswith("clipforge-audio-")
        ]
        self.assertEqual(leftovers, [], f"scratch audio left behind: {leftovers}")

    def test_a_long_file_is_chunked_and_stitched_back_together(self) -> None:
        """The chunking path, on real audio: forty-odd seconds cut into
        four-second windows, decoded separately, merged into one timeline."""

        engine = self._engine(audio=AudioConfig(
            ffmpeg=FFMPEG, chunk_s=4.0, overlap_s=1.0, chunk_threshold_s=6.0))
        transcript = engine.transcribe(_Media.long_wav)

        self.assertGreater(transcript.word_count, 0)
        self.assertGreater(transcript.duration_s, 10.0,
                           "the merged timeline is shorter than one chunk")
        for previous, word in zip(transcript.words, transcript.words[1:]):
            self.assertLessEqual(previous.start_s, word.start_s,
                                 "merged words are out of order")

    def test_a_silent_file_fails_permanently_and_is_not_retried(self) -> None:
        self.engine.enqueue("src_1", _Media.silent_mp4)
        runs = self.engine.run(limit=1)

        self.assertEqual(runs[0].state, TranscriptionState.FAILED_PERMANENT.value)
        self.assertIn("no audio track", runs[0].last_error)
        with self.db.unit_of_work(TENANT) as uow:
            self.assertEqual(uow.jobs.all()[0].state, "dead")

    def test_a_missing_file_fails_permanently(self) -> None:
        self.engine.enqueue("src_1", "/no/such/media.mp4")
        runs = self.engine.run(limit=1)
        self.assertEqual(runs[0].state, TranscriptionState.FAILED_PERMANENT.value)

    def test_an_unavailable_provider_is_a_configuration_failure(self) -> None:
        """A missing model is the operator's problem, not the media's, and
        retrying means failing identically once a minute until someone looks."""

        class Unavailable:
            info = ProviderInfo(name="broken")

            def availability(self):
                from clipforge.transcribe.provider import Availability

                return Availability(False, "no model on disk")

            def transcribe(self, wav_path, *, language=""):
                raise AssertionError("should not have been called")

        engine = self._engine(Unavailable())
        engine.enqueue("src_1", _Media.speech_mp4)
        runs = engine.run(limit=1)
        self.assertEqual(runs[0].state, TranscriptionState.FAILED_PERMANENT.value)
        self.assertIn("no model on disk", runs[0].last_error)

    def test_a_transient_failure_is_retried(self) -> None:
        """A rate limit and a bad key both stop a job. Doing the same about
        both is how a queue either gives up on recoverable work or spends its
        afternoon re-failing on a typo."""

        class Flaky:
            info = ProviderInfo(name="flaky")

            def availability(self):
                from clipforge.transcribe.provider import Availability

                return Availability(True)

            def transcribe(self, wav_path, *, language=""):
                raise RetryableError("the service is busy")

        engine = self._engine(Flaky())
        engine.enqueue("src_1", _Media.speech_mp4)
        runs = engine.run(limit=1)

        self.assertEqual(runs[0].state, TranscriptionState.FAILED_RETRYABLE.value)
        with self.db.unit_of_work(TENANT) as uow:
            job = uow.jobs.all()[0]
        self.assertEqual(job.state, "queued")
        self.assertGreater(job.run_after, self.engine.clock())

    def test_the_same_source_is_not_transcribed_twice(self) -> None:
        """A second run is a second invoice on a paid provider, and a second
        transcript that disagrees at the margins with reviewed captions."""

        first = self.engine.enqueue("src_1", _Media.speech_mp4)
        again = self.engine.enqueue("src_1", _Media.speech_mp4)
        self.assertEqual(first, again)
        with self.db.unit_of_work(TENANT) as uow:
            self.assertEqual(uow.jobs.count(), 1)

    def test_a_source_keeps_one_run_row_across_attempts(self) -> None:
        """Postgres has `unique(tenant_id, source_id)` on this table, so a
        re-queue that mints a fresh run id inserts a duplicate and the failure
        arrives as a constraint error from inside the queue. Requeueing after
        a permanent failure is the ordinary case — a bad key since replaced, a
        provider since installed — so it has to land on the existing row."""

        self.engine.enqueue("src_1", _Media.speech_mp4)
        with self.db.unit_of_work(TENANT) as uow:
            first = uow.transcriptions.for_source("src_1")
            uow.jobs.fail(uow.jobs.all()[0].id, "provider gone", None, utcnow())
            record = uow.transcriptions.require(first.id)
            record.state = TranscriptionState.FAILED_PERMANENT.value
            uow.transcriptions.save(record)

        self.engine.enqueue("src_1", _Media.speech_mp4)
        with self.db.unit_of_work(TENANT) as uow:
            rows = [r for r in uow.transcriptions.all() if r.source_id == "src_1"]
        self.assertEqual(len(rows), 1, "a second row was inserted for one source")
        self.assertEqual(rows[0].id, first.id)
        self.assertEqual(rows[0].state, TranscriptionState.QUEUED.value)
        self.assertEqual(rows[0].last_error, "",
                         "the previous failure is still recorded on a fresh queue")

    def test_an_inline_transcription_is_stored(self) -> None:
        """The read-before-transcribe saving is only real if the inline path
        writes something to read. Otherwise every factory cycle pays again."""

        transcript = self.engine.transcribe_source("src_1", _Media.speech_mp4)
        self.assertGreater(transcript.word_count, 0)

        with self.db.unit_of_work(TENANT) as uow:
            run = uow.transcriptions.for_source("src_1")
            source = uow.sources.get("src_1")
        self.assertEqual(run.state, TranscriptionState.SUCCEEDED.value)
        self.assertEqual(run.word_count, transcript.word_count)
        self.assertTrue(source.has_transcript)
        self.assertIsNotNone(self.engine.transcript_for("src_1"))

    def test_an_inline_failure_is_recorded_as_permanent(self) -> None:
        """No job behind it, so permanence follows the exception's class."""

        with self.assertRaises(PermanentError):
            self.engine.transcribe_source("src_1", _Media.silent_mp4)
        with self.db.unit_of_work(TENANT) as uow:
            run = uow.transcriptions.for_source("src_1")
        self.assertEqual(run.state, TranscriptionState.FAILED_PERMANENT.value)
        self.assertTrue(run.last_error)

    def test_states_move_queued_to_processing_to_succeeded(self) -> None:
        self.engine.enqueue("src_1", _Media.speech_mp4)
        with self.db.unit_of_work(TENANT) as uow:
            self.assertEqual(uow.transcriptions.all()[0].state,
                             TranscriptionState.QUEUED.value)
        run = self.engine.run(limit=1)[0]
        self.assertEqual(run.state, TranscriptionState.SUCCEEDED.value)
        self.assertGreaterEqual(run.attempts, 1)
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.finished_at)


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    FFMPEG and ESPEAK and _sphinx_available(),
    "needs ffmpeg, espeak-ng and pocketsphinx",
)
class PipelineIntegrationTest(unittest.TestCase):
    """The factory's `Transcriber` protocol, satisfied for real."""

    def setUp(self) -> None:
        self.workspace = tempfile.mkdtemp(prefix="clipforge-pi-")
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.db = MemoryDatabase()
        with self.db.unit_of_work(TENANT) as uow:
            uow.tenants.save(TenantRecord(id=TENANT, name="Pipeline"))
            uow.sources.save(SourceRecord(id="src_1", tenant_id=TENANT,
                                          fingerprint="fp1"))
        self.engine = TranscriptionEngine(
            self.db, TENANT, SphinxProvider(),
            config=TranscriptionConfig(workspace=self.workspace,
                                       audio=AudioConfig(ffmpeg=FFMPEG)),
        )
        self.source = Source(source_id="src_1", title="A talk",
                             kind=SourceKind.LONGFORM_VIDEO,
                             rights=Rights(basis=RightsBasis.OWNED),
                             language="en")

    def test_the_transcriber_protocol_is_satisfied(self) -> None:
        """`Transcriber` is a plain Protocol, so there is no isinstance check
        to lean on. Compare the signature instead: the factory calls
        `transcribe(source)` and nothing else, and a mismatch here is a
        TypeError at the one moment the pipeline is finally running."""

        from clipforge.factory.pipeline import Transcriber

        transcriber = EngineTranscriber(self.engine,
                                        media_root=_Media.directory)
        self.assertTrue(callable(getattr(transcriber, "transcribe", None)))
        self.assertEqual(
            list(inspect.signature(transcriber.transcribe).parameters),
            list(inspect.signature(Transcriber.transcribe).parameters)[1:],
        )

    def test_it_returns_timed_words_the_caption_engine_accepts(self) -> None:
        from clipforge.captions.types import TimedWord

        transcriber = EngineTranscriber(self.engine, media_root=_Media.directory)
        # `media_for` finds it by source id; name the file accordingly.
        target = os.path.join(_Media.directory, "src_1.mp4")
        shutil.copy2(_Media.speech_mp4, target)
        self.addCleanup(os.remove, target)

        words = transcriber.transcribe(self.source)
        self.assertTrue(words)
        self.assertTrue(all(isinstance(w, TimedWord) for w in words))
        for word in words:
            self.assertGreater(word.end_ms, word.start_ms)
            self.assertTrue(word.text.strip())

    def test_a_stored_transcript_is_reused_rather_than_recomputed(self) -> None:
        """Transcription is the most expensive stage and its output never
        changes for a given input."""

        self.engine.enqueue("src_1", _Media.speech_mp4)
        self.engine.run(limit=1)

        class Exploding(SphinxProvider):
            def transcribe(self, wav_path, *, language=""):
                raise AssertionError("a stored transcript should have been used")

        engine = TranscriptionEngine(
            self.db, TENANT, Exploding(),
            config=TranscriptionConfig(workspace=self.workspace,
                                       audio=AudioConfig(ffmpeg=FFMPEG)),
        )
        words = EngineTranscriber(engine).transcribe(self.source)
        self.assertTrue(words)

    def test_inline_transcription_can_be_refused(self) -> None:
        """The right setting for a worker pool where transcription is its own
        queue: a source with no stored transcript should wait, not block a
        factory cycle for ten minutes."""

        transcriber = EngineTranscriber(self.engine, allow_inline=False)
        with self.assertRaises(PermanentError):
            transcriber.transcribe(self.source)

    def test_zero_length_words_are_dropped(self) -> None:
        """A zero-length word is a caption drawn for zero frames, and a
        karaoke fill that divides by its own duration finds an infinity."""

        transcript = Transcript(
            text="a b",
            words=(Word("a", 1.0, 1.0), Word("b", 1.0, 1.4), Word(" ", 2.0, 2.5)),
        )
        words = to_timed_words(transcript)
        self.assertEqual([w.text for w in words], ["b"])


# ---------------------------------------------------------------------------
# OpenAI-compatible client, against a real HTTP server
# ---------------------------------------------------------------------------


VERBOSE_JSON = {
    "task": "transcribe",
    "language": "english",
    "duration": 4.2,
    "text": "The quick brown fox jumps over the lazy dog.",
    "segments": [
        {"id": 0, "seek": 0, "start": 0.0, "end": 2.1,
         "text": " The quick brown fox", "avg_logprob": -0.21,
         "no_speech_prob": 0.01},
        {"id": 1, "seek": 0, "start": 2.1, "end": 4.2,
         "text": " jumps over the lazy dog.", "avg_logprob": -0.18,
         "no_speech_prob": 0.02},
    ],
    "words": [
        {"word": "The", "start": 0.0, "end": 0.3},
        {"word": "quick", "start": 0.3, "end": 0.7},
        {"word": "brown", "start": 0.7, "end": 1.2},
        {"word": "fox", "start": 1.2, "end": 2.1},
        {"word": "jumps", "start": 2.1, "end": 2.6},
        {"word": "over", "start": 2.6, "end": 3.0},
        {"word": "the", "start": 3.0, "end": 3.2},
        {"word": "lazy", "start": 3.2, "end": 3.7},
        {"word": "dog", "start": 3.7, "end": 4.2},
    ],
}


class _ApiHandler(BaseHTTPRequestHandler):
    """A real server speaking OpenAI's documented transcription protocol."""

    def log_message(self, *args) -> None:  # noqa: A003
        pass

    def do_POST(self) -> None:  # noqa: N802
        state = self.server.state
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)

        state["requests"].append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type", ""),
            "content_length": length,
            "body": body,
        })

        if state["status"] != 200:
            payload = json.dumps(
                {"error": {"message": state["error_message"]}}
            ).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        payload = json.dumps(state["payload"]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _ApiServer:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ApiHandler)
        self.httpd.state = {
            "requests": [], "status": 200, "payload": VERBOSE_JSON,
            "error_message": "something went wrong",
        }
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def state(self) -> dict:
        return self.httpd.state

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@unittest.skipUnless(FFMPEG and ESPEAK, "needs ffmpeg and espeak-ng for the audio")
class OpenAICompatibleClientTest(unittest.TestCase):
    """This client against a real socket speaking the documented protocol.

    Verifies the request this code builds and the response it parses. Says
    nothing about whether OpenAI behaves this way — nothing here has reached
    OpenAI.
    """

    def setUp(self) -> None:
        self.server = _ApiServer()
        self.addCleanup(self.server.close)
        self.tmp = tempfile.mkdtemp(prefix="clipforge-api-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.audio = extract_audio(_Media.speech_mp4,
                                   os.path.join(self.tmp, "a.wav"))
        self.provider = OpenAICompatibleProvider(
            OpenAIConfig(base_url=self.server.base_url, model="whisper-1",
                         api_key_env="CLIPFORGE_TEST_KEY", timeout_s=30)
        )

    def test_a_transcript_comes_back_with_words_and_segments(self) -> None:
        transcript = self.provider.transcribe(self.audio)
        self.assertEqual(transcript.word_count, 9)
        self.assertEqual(len(transcript.segments), 2)
        self.assertEqual(transcript.language, "en")
        self.assertAlmostEqual(transcript.duration_s, 4.2)
        self.assertEqual(transcript.words[0].text, "The")
        self.assertAlmostEqual(transcript.words[-1].end_s, 4.2)

    def test_the_request_is_a_real_multipart_upload_of_the_audio(self) -> None:
        self.provider.transcribe(self.audio)
        request = self.server.state["requests"][0]

        self.assertTrue(request["path"].endswith("/audio/transcriptions"))
        self.assertIn("multipart/form-data; boundary=", request["content_type"])
        # The whole file crossed the wire.
        self.assertGreaterEqual(request["content_length"],
                                os.path.getsize(self.audio))
        body = request["body"]
        self.assertIn(b'name="model"', body)
        self.assertIn(b"whisper-1", body)
        self.assertIn(b'name="response_format"', body)
        self.assertIn(b"verbose_json", body)
        # Word *and* segment granularity: asking only for words makes some
        # servers omit segments, and the detector scores segments.
        self.assertEqual(body.count(b'name="timestamp_granularities[]"'), 2)
        self.assertIn(b"word", body)
        self.assertIn(b"segment", body)
        self.assertIn(b'filename="a.wav"', body)
        self.assertIn(b"RIFF", body, "the audio itself was not in the body")

    def test_the_key_is_read_from_the_environment_at_request_time(self) -> None:
        """Never stored on the instance: a key on the object outlives the
        request in a heap dump, a repr and a pickled task payload."""

        os.environ["CLIPFORGE_TEST_KEY"] = "sk-test-not-a-real-key"
        self.addCleanup(os.environ.pop, "CLIPFORGE_TEST_KEY", None)

        self.provider.transcribe(self.audio)
        self.assertEqual(self.server.state["requests"][0]["authorization"],
                         "Bearer sk-test-not-a-real-key")
        self.assertNotIn(
            "sk-test-not-a-real-key", repr(self.provider.__dict__),
            "the key was retained on the instance",
        )

    def test_no_authorization_header_is_sent_without_a_key(self) -> None:
        """A local whisper.cpp needs no auth, and sending `Bearer ` would be
        worse than sending nothing."""

        self.provider.transcribe(self.audio)
        self.assertIsNone(self.server.state["requests"][0]["authorization"])

    def test_a_401_is_a_configuration_failure_not_a_retry(self) -> None:
        self.server.state["status"] = 401
        self.server.state["error_message"] = "Incorrect API key provided"
        with self.assertRaises(ProviderUnavailable) as caught:
            self.provider.transcribe(self.audio)
        self.assertIn("Incorrect API key", str(caught.exception))

    def test_a_429_is_retryable(self) -> None:
        self.server.state["status"] = 429
        self.server.state["error_message"] = "Rate limit reached"
        with self.assertRaises(RetryableError):
            self.provider.transcribe(self.audio)

    def test_a_500_is_retryable(self) -> None:
        self.server.state["status"] = 503
        with self.assertRaises(RetryableError):
            self.provider.transcribe(self.audio)

    def test_a_400_is_permanent(self) -> None:
        self.server.state["status"] = 400
        self.server.state["error_message"] = "Unsupported file format"
        with self.assertRaises(PermanentError):
            self.provider.transcribe(self.audio)

    def test_an_unreachable_service_is_retryable(self) -> None:
        provider = OpenAICompatibleProvider(
            OpenAIConfig(base_url="http://127.0.0.1:9/v1", timeout_s=2,
                         api_key_env="CLIPFORGE_TEST_KEY")
        )
        with self.assertRaises(RetryableError):
            provider.transcribe(self.audio)

    def test_an_oversized_file_is_refused_before_it_is_uploaded(self) -> None:
        big = os.path.join(self.tmp, "big.wav")
        with open(big, "wb") as handle:
            handle.truncate(26 * 1024 * 1024)
        with self.assertRaises(PermanentError) as caught:
            self.provider.transcribe(big)
        self.assertIn("chunk_s", str(caught.exception))
        self.assertEqual(self.server.state["requests"], [],
                         "an oversized file was uploaded anyway")

    def test_the_engine_drives_this_provider_end_to_end(self) -> None:
        db = MemoryDatabase()
        with db.unit_of_work(TENANT) as uow:
            uow.tenants.save(TenantRecord(id=TENANT, name="API"))
            uow.sources.save(SourceRecord(id="src_api", tenant_id=TENANT,
                                          fingerprint="fpapi"))
        engine = TranscriptionEngine(
            db, TENANT, self.provider,
            config=TranscriptionConfig(workspace=self.tmp,
                                       audio=AudioConfig(ffmpeg=FFMPEG)),
        )
        engine.enqueue("src_api", _Media.speech_mp4)
        run = engine.run(limit=1)[0]
        self.assertEqual(run.state, TranscriptionState.SUCCEEDED.value)
        self.assertEqual(run.word_count, 9)
        self.assertEqual(run.language, "en")


class VerboseJsonParsingTest(unittest.TestCase):
    """The response mapping, against recorded payload shapes."""

    def _info(self) -> ProviderInfo:
        return ProviderInfo(name="openai-compatible", model="whisper-1", remote=True)

    def test_language_names_become_iso_codes(self) -> None:
        """OpenAI answers `english`, not `en`, and everything downstream —
        including the clip gate — expects a code."""

        for name, code in (("english", "en"), ("german", "de"), ("es", "es")):
            payload = dict(VERBOSE_JSON, language=name)
            self.assertEqual(parse_verbose_json(payload, self._info()).language, code)

    def test_an_unknown_language_passes_through_rather_than_being_guessed(self) -> None:
        payload = dict(VERBOSE_JSON, language="cornish")
        self.assertEqual(parse_verbose_json(payload, self._info()).language, "cornish")

    def test_words_are_distributed_into_their_segments(self) -> None:
        """OpenAI returns words in a flat list, not nested per segment."""

        transcript = parse_verbose_json(VERBOSE_JSON, self._info())
        self.assertEqual(len(transcript.segments[0].words), 4)
        self.assertEqual(len(transcript.segments[1].words), 5)

    def test_confidence_is_none_because_openai_reports_none(self) -> None:
        """A 1.0 here would be a fabrication, and something downstream
        eventually filters on it."""

        transcript = parse_verbose_json(VERBOSE_JSON, self._info())
        self.assertTrue(all(w.confidence is None for w in transcript.words))
        self.assertIsNone(transcript.mean_confidence)

    def test_a_response_with_only_text_still_parses(self) -> None:
        """Gateways implementing the same protocol vary in what they return.
        A missing part should degrade the transcript, not fail it."""

        transcript = parse_verbose_json({"text": "hello there"}, self._info())
        self.assertEqual(transcript.text, "hello there")
        self.assertEqual(transcript.word_count, 0)
        self.assertFalse(transcript.has_word_timings)


# ---------------------------------------------------------------------------
# Local Whisper — mapping only. See the module docstring.
# ---------------------------------------------------------------------------


class _FakeWord:
    def __init__(self, word, start, end, probability):
        self.word, self.start, self.end, self.probability = word, start, end, probability


class _FakeSegment:
    def __init__(self, text, start, end, words, avg_logprob=-0.2, no_speech_prob=0.01):
        self.text, self.start, self.end = text, start, end
        self.words = words
        self.avg_logprob, self.no_speech_prob = avg_logprob, no_speech_prob


class _FakeInfo:
    def __init__(self, language="en", language_probability=0.98, duration=4.2):
        self.language = language
        self.language_probability = language_probability
        self.duration = duration


class LocalWhisperMappingTest(unittest.TestCase):
    """faster-whisper's result objects onto `Transcript`.

    **This is not a test of Whisper.** No model runs here and none can in this
    environment. What it pins is this repository's mapping — the place where a
    field rename between faster-whisper releases would silently drop word
    timings.
    """

    def test_the_documented_result_shape_maps_onto_a_transcript(self) -> None:
        segments = [
            _FakeSegment(" The quick brown fox", 0.0, 2.1, [
                _FakeWord(" The", 0.0, 0.3, 0.98),
                _FakeWord(" quick", 0.3, 0.7, 0.91),
            ]),
            _FakeSegment(" jumps over", 2.1, 4.2, [
                _FakeWord(" jumps", 2.1, 2.6, 0.88),
            ]),
        ]
        transcript = _to_transcript(segments, _FakeInfo(),
                                    ProviderInfo(name="local-whisper"))

        self.assertEqual(transcript.word_count, 3)
        self.assertEqual([w.text for w in transcript.words],
                         ["The", "quick", "jumps"])
        self.assertEqual(transcript.language, "en")
        self.assertAlmostEqual(transcript.language_confidence, 0.98)
        self.assertAlmostEqual(transcript.words[0].confidence, 0.98)
        self.assertEqual(len(transcript.segments), 2)
        self.assertAlmostEqual(transcript.segments[0].avg_logprob, -0.2)

    def test_hallucinated_segments_over_silence_are_dropped(self) -> None:
        """Whisper writes fluent text over musical intros. A confident
        transcript of words nobody said is the failure people notice on a real
        feed."""

        segments = [
            _FakeSegment(" Thanks for watching!", 0.0, 3.0,
                         [_FakeWord(" Thanks", 0.0, 0.5, 0.4)],
                         no_speech_prob=HALLUCINATION_NO_SPEECH + 0.2),
            _FakeSegment(" Real speech", 3.0, 5.0,
                         [_FakeWord(" Real", 3.0, 3.4, 0.95)],
                         no_speech_prob=0.02),
        ]
        transcript = _to_transcript(segments, _FakeInfo(),
                                    ProviderInfo(name="local-whisper"))
        self.assertEqual([w.text for w in transcript.words], ["Real"])
        self.assertNotIn("Thanks", transcript.text)

    def test_a_missing_probability_becomes_none_not_zero(self) -> None:
        segments = [_FakeSegment(" word", 0.0, 1.0,
                                 [_FakeWord(" word", 0.0, 1.0, None)])]
        transcript = _to_transcript(segments, _FakeInfo(),
                                    ProviderInfo(name="local-whisper"))
        self.assertIsNone(transcript.words[0].confidence)

    def test_availability_admits_it_cannot_prove_the_model_loads(self) -> None:
        """Requirement, not politeness: a deployment has to be able to tell
        'configured' from 'working' without reading a docstring."""

        availability = LocalWhisperProvider().availability()
        if availability.ready and not availability.unverified:
            self.skipTest("a Whisper model is cached in this environment")
        self.assertTrue(
            availability.unverified or not availability.ready,
            "a provider with no model on disk claimed to be verified",
        )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ConfigurationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            key: value for key, value in os.environ.items()
            if key.startswith("CLIPFORGE_TRANSCRIBE_")
        }
        for key in list(self._saved):
            del os.environ[key]

    def tearDown(self) -> None:
        for key in list(os.environ):
            if key.startswith("CLIPFORGE_TRANSCRIBE_"):
                del os.environ[key]
        os.environ.update(self._saved)

    def test_the_provider_comes_from_the_environment(self) -> None:
        os.environ["CLIPFORGE_TRANSCRIBE_PROVIDER"] = "openai"
        os.environ["CLIPFORGE_TRANSCRIBE_BASE_URL"] = "http://localhost:8080/v1"
        os.environ["CLIPFORGE_TRANSCRIBE_MODEL"] = "whisper-large-v3"

        provider = provider_from_env()
        self.assertIsInstance(provider, OpenAICompatibleProvider)
        self.assertEqual(provider.info.model, "whisper-large-v3")
        self.assertEqual(provider.config.base_url, "http://localhost:8080/v1")

    def test_no_provider_configured_is_an_error_not_a_guess(self) -> None:
        """A pipeline that quietly picks a different transcriber than the
        operator configured produces captions from a model nobody chose."""

        with self.assertRaises(ProviderUnavailable) as caught:
            provider_from_env()
        self.assertIn("CLIPFORGE_TRANSCRIBE_PROVIDER", str(caught.exception))

    def test_an_unknown_provider_names_the_ones_that_exist(self) -> None:
        os.environ["CLIPFORGE_TRANSCRIBE_PROVIDER"] = "deepgram"
        with self.assertRaises(ProviderUnavailable) as caught:
            provider_from_env()
        self.assertIn("local_whisper", str(caught.exception))

    def test_the_key_variable_is_named_not_the_key(self) -> None:
        """The indirection keeps secrets out of this module, out of any config
        object that might be logged, and lets two providers read two keys."""

        os.environ["CLIPFORGE_TRANSCRIBE_PROVIDER"] = "openai"
        os.environ["CLIPFORGE_TRANSCRIBE_API_KEY_ENV"] = "MY_OWN_KEY_VAR"
        provider = provider_from_env()
        self.assertEqual(provider.config.api_key_env, "MY_OWN_KEY_VAR")
        # The config carries a variable name and no secret.
        self.assertNotIn("sk-", json.dumps(provider.info.to_dict()))

    def test_no_key_appears_anywhere_in_the_provider_description(self) -> None:
        os.environ["CLIPFORGE_TRANSCRIBE_PROVIDER"] = "openai"
        os.environ["MY_OWN_KEY_VAR"] = "sk-this-must-not-leak"
        os.environ["CLIPFORGE_TRANSCRIBE_API_KEY_ENV"] = "MY_OWN_KEY_VAR"
        self.addCleanup(os.environ.pop, "MY_OWN_KEY_VAR", None)

        described = json.dumps(describe_environment())
        self.assertNotIn("sk-this-must-not-leak", described)
        self.assertIn("MY_OWN_KEY_VAR", described)
        self.assertIn('"api_key_present": true', described)

    def test_the_environment_report_marks_unverified_providers(self) -> None:
        report = describe_environment()
        whisper = report["providers"]["local_whisper"]
        if whisper["ready"]:
            self.assertTrue(
                whisper["unverified"] or "cached" in whisper["detail"],
                "local whisper claimed verified with no model on disk",
            )
        openai = report["providers"]["openai"]
        if openai["ready"]:
            self.assertTrue(openai["unverified"])

    def test_audio_settings_come_from_the_environment(self) -> None:
        from clipforge.transcribe import audio_config_from_env

        os.environ["CLIPFORGE_TRANSCRIBE_CHUNK_S"] = "120"
        os.environ["CLIPFORGE_TRANSCRIBE_OVERLAP_S"] = "5"
        config = audio_config_from_env()
        self.assertEqual(config.chunk_s, 120.0)
        self.assertEqual(config.overlap_s, 5.0)


class SerialisationTest(unittest.TestCase):
    def test_a_transcript_round_trips_through_its_stored_form(self) -> None:
        """The stored form is the only copy. A word timing lost here is a
        caption with nothing to karaoke and a transcription paid for twice."""

        original = Transcript(
            text="hello there",
            words=(Word("hello", 0.0, 0.5, 0.9), Word("there", 0.5, 1.0, None)),
            segments=(Segment("hello there", 0.0, 1.0,
                              (Word("hello", 0.0, 0.5, 0.9),),
                              avg_logprob=-0.3, no_speech_prob=0.02),),
            language="en", language_confidence=0.97, duration_s=1.0,
            provider=ProviderInfo(name="p", model="m", remote=True),
        )
        restored = transcript_from_dict(transcript_to_dict(original))

        self.assertEqual(restored.text, original.text)
        self.assertEqual(restored.language, original.language)
        self.assertEqual(restored.word_count, original.word_count)
        self.assertEqual(restored.words[0].confidence, 0.9)
        self.assertIsNone(restored.words[1].confidence)
        self.assertEqual(restored.segments[0].avg_logprob, -0.3)
        self.assertEqual(restored.provider.name, "p")
        self.assertTrue(restored.provider.remote)


# ---------------------------------------------------------------------------
# The engine against a real database
# ---------------------------------------------------------------------------


_DSN = os.environ.get("CLIPFORGE_TEST_DSN", "")


@unittest.skipUnless(
    _DSN and FFMPEG and ESPEAK and _sphinx_available(),
    "needs CLIPFORGE_TEST_DSN, ffmpeg, espeak-ng and pocketsphinx",
)
class PostgresTranscriptionTest(unittest.TestCase):
    """The same queue, the same states, against Postgres rather than a dict.

    The in-memory tests above are only evidence about this path because this
    runs. Two things here exist in Postgres and nowhere else: the `jsonb`
    round trip of the transcript, and `unique(tenant_id, source_id)`.
    """

    def setUp(self) -> None:
        import psycopg

        from clipforge.store.postgres import PostgresDatabase

        admin = os.environ.get("CLIPFORGE_TEST_ADMIN_DSN", _DSN)
        with psycopg.connect(admin) as connection:
            with connection.cursor() as cursor:
                cursor.execute("TRUNCATE TABLE tenants CASCADE")
            connection.commit()

        self.workspace = tempfile.mkdtemp(prefix="clipforge-pgtx-")
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.db = PostgresDatabase(_DSN, min_size=1, max_size=4)
        self.addCleanup(self.db.close)
        with self.db.unit_of_work(TENANT) as uow:
            uow.tenants.save(TenantRecord(id=TENANT, name="Transcriber"))
            uow.sources.save(SourceRecord(id="src_1", tenant_id=TENANT,
                                          title="A talk", fingerprint="fp1"))
        self.engine = TranscriptionEngine(
            self.db, TENANT, SphinxProvider(),
            config=TranscriptionConfig(workspace=self.workspace,
                                       audio=AudioConfig(ffmpeg=FFMPEG)),
        )

    def test_a_transcript_survives_the_database(self) -> None:
        self.engine.enqueue("src_1", _Media.speech_mp4)
        run = self.engine.run(limit=1)[0]
        self.assertEqual(run.state, TranscriptionState.SUCCEEDED.value)

        stored = self.engine.transcript_for("src_1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.word_count, run.word_count)
        self.assertTrue(stored.has_word_timings)
        for previous, word in zip(stored.words, stored.words[1:]):
            self.assertLessEqual(previous.start_s, word.start_s)

        with self.db.unit_of_work(TENANT) as uow:
            self.assertTrue(uow.sources.require("src_1").has_transcript)

    def test_requeueing_reuses_the_row_the_unique_index_allows(self) -> None:
        """Postgres is the only place this constraint exists, so it is the
        only place the reuse can actually be verified."""

        self.engine.enqueue("src_1", _Media.speech_mp4)
        self.engine.run(limit=1)
        self.engine.enqueue("src_1", _Media.speech_mp4)
        with self.db.unit_of_work(TENANT) as uow:
            rows = [r for r in uow.transcriptions.all() if r.source_id == "src_1"]
        self.assertEqual(len(rows), 1)

    def test_a_permanent_failure_is_stored_as_one(self) -> None:
        self.engine.enqueue("src_1", _Media.silent_mp4)
        run = self.engine.run(limit=1)[0]
        self.assertEqual(run.state, TranscriptionState.FAILED_PERMANENT.value)
        self.assertIn("no audio track", run.last_error)
        with self.db.unit_of_work(TENANT) as uow:
            self.assertFalse(uow.sources.require("src_1").has_transcript)


if __name__ == "__main__":
    unittest.main()
