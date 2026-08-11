"""The transcription layer's front door.

    engine = TranscriptionEngine(db, "ten_acme", provider=provider_from_env())
    engine.enqueue(source_id, media_path)
    engine.run(limit=2)

    transcript = engine.transcript_for(source_id)   # from Postgres, later

`enqueue` queues; `run` drains. Transcription is minutes of work per hour of
audio even on a GPU, so the caller queueing is never the caller waiting.

## The shape of one job

    extract 16 kHz mono   ->  chunk if long  ->  provider per chunk
      ->  merge with offsets  ->  persist  ->  delete the scratch audio

Every step is bounded. The extraction streams through ffmpeg; the chunks are
ten minutes each; the merge holds one transcript's worth of words. A
three-hour podcast never has more than one chunk of audio on disk at a time
and never has any of it in memory.

## States

`queued` → `processing` → `succeeded`, or one of two failures. The split
between `failed_retryable` and `failed_permanent` is the whole point of the
state machine: a rate limit and a missing API key both stop a job, and doing
the same thing about both is how a queue either gives up on recoverable work
or spends its afternoon re-failing on a typo'd key.

## What is persisted

The `transcription_runs` row carries the state, the attempt count, the
provider, the language, the counts and the error. The transcript itself —
every word with its timings — goes in the same row as jsonb, because it is
read as a unit and never queried into.

The transcript is also written onto the `sources` row as `has_transcript`, so
the factory's existing clearance query keeps working without knowing this
layer exists.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from ..publish.types import utcnow
from ..store.records import JobRecord, TranscriptionRunRecord
from .audio import (
    AudioChunk,
    AudioConfig,
    extract_chunk,
    plan_chunks,
    wav_duration_s,
)
from .provider import TranscriptionProvider, merge_chunks
from .types import (
    AudioExtractionFailed,
    PermanentError,
    ProviderUnavailable,
    RetryableError,
    Segment,
    Transcript,
    TranscriptionError,
    TranscriptionState,
    Word,
)

__all__ = [
    "TranscriptionConfig",
    "TranscriptionEngine",
    "TRANSCRIBE_JOB",
    "transcript_to_dict",
    "transcript_from_dict",
]

#: The job kind this engine claims from the shared queue.
TRANSCRIBE_JOB = "transcribe"


@dataclass(slots=True)
class TranscriptionConfig:
    workspace: str = "/var/lib/clipforge/transcribe"
    audio: AudioConfig | None = None
    max_attempts: int = 4
    retry_base_s: int = 120
    #: Generous, because the lease has to outlast the work: a three-hour
    #: podcast on a CPU model is genuinely an hour, and a lease that expires
    #: mid-job hands the same work to a second worker.
    lease_s: int = 7200
    #: Keep the extracted audio next to the run. Off by default — it is
    #: hundreds of megabytes per source — but the first thing anyone wants
    #: when a transcript reads badly.
    keep_audio: bool = False
    #: Refuse a transcript below this share of words carrying text. Catches a
    #: provider that returned segments but no words, which produces captions
    #: with nothing to karaoke.
    min_words: int = 1


class TranscriptionEngine:
    """Extract, transcribe, merge, persist."""

    def __init__(
        self,
        database: Any,
        tenant_id: str,
        provider: TranscriptionProvider,
        *,
        config: TranscriptionConfig | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if not tenant_id:
            raise ValueError("transcription needs a tenant")
        self.database = database
        self.tenant_id = tenant_id
        self.provider = provider
        self.config = config or TranscriptionConfig()
        self.audio = self.config.audio or AudioConfig()
        self.clock = clock

    # -- queueing ----------------------------------------------------------

    def enqueue(
        self,
        source_id: str,
        media_path: str,
        *,
        language: str = "",
        channel_id: str = "",
        priority: int = 100,
    ) -> str:
        """Queue one transcription. Returns the job id."""

        run_id = self._run_id_for(source_id)
        with self.database.unit_of_work(self.tenant_id) as uow:
            job = uow.jobs.enqueue(JobRecord(
                id=f"job_{uuid.uuid4().hex[:12]}",
                tenant_id=self.tenant_id,
                channel_id=channel_id or None,
                kind=TRANSCRIBE_JOB,
                priority=priority,
                run_after=self.clock(),
                max_attempts=self.config.max_attempts,
                payload={
                    "run_id": run_id,
                    "source_id": source_id,
                    "media_path": media_path,
                    "language": language,
                },
                # One transcription per source. Transcribing the same podcast
                # twice costs real money on a paid provider and produces two
                # transcripts that disagree at the margins.
                dedupe_key=f"transcribe:{source_id}",
            ))
            record = uow.transcriptions.get(run_id) or TranscriptionRunRecord(
                id=run_id, tenant_id=self.tenant_id, source_id=source_id or None
            )
            record.state = TranscriptionState.QUEUED.value
            record.media_path = media_path
            record.language = language
            record.provider = self.provider.info.name
            record.model = self.provider.info.model
            record.last_error = ""
            record.finished_at = None
            uow.transcriptions.save(record)
        return job.id

    def _run_id_for(self, source_id: str) -> str:
        """The run row a source already has, or a new id.

        A source has one transcription row, not one per attempt: re-queueing
        after a permanent failure — a bad key since replaced, a provider since
        installed — must land on the row that already exists. Minting a fresh
        id would insert a second row for the same source, which Postgres
        refuses outright on `unique(tenant_id, source_id)`, and the refusal
        would arrive as a constraint error from inside the queue rather than
        as anything an operator could act on.
        """

        if source_id:
            with self.database.unit_of_work(self.tenant_id) as uow:
                existing = uow.transcriptions.for_source(source_id)
            if existing is not None:
                return existing.id
        return f"txn_{uuid.uuid4().hex[:12]}"

    def transcribe_source(
        self, source_id: str, media_path: str, *, language: str = ""
    ) -> Transcript:
        """Transcribe now, and store the result against the source.

        The inline counterpart to `enqueue` + `run`, for the caller who has one
        source in hand and no worker running. It persists for the same reason
        the queued path does: an inline transcript that is thrown away is
        recomputed on the next pipeline cycle, which on a paid provider is a
        second invoice for an answer that cannot have changed.
        """

        run_id = self._run_id_for(source_id)
        self._mark(run_id, TranscriptionState.PROCESSING, source_id=source_id,
                   media_path=media_path)
        started = time.perf_counter()
        try:
            transcript = self.transcribe(media_path, language=language)
        except Exception as error:                              # noqa: BLE001
            # No job, so no queue to decide whether another attempt is
            # allowed. Permanence follows the exception's own class, which is
            # what it is for.
            self._fail_inline(
                run_id, str(error),
                permanent=isinstance(error, (PermanentError, ProviderUnavailable)),
            )
            raise
        self._succeed(run_id, transcript, time.perf_counter() - started, source_id)
        return transcript

    def run(
        self, limit: int = 1, *, worker_id: str = "transcribe-1"
    ) -> list[TranscriptionRunRecord]:
        """Claim queued transcriptions and carry them out."""

        now = self.clock()
        with self.database.unit_of_work(self.tenant_id) as uow:
            claimed = uow.jobs.claim(
                worker_id, now, lease_s=self.config.lease_s,
                kinds=(TRANSCRIBE_JOB,), limit=limit,
            )
        return [self._run_job(job) for job in claimed]

    def _run_job(self, job: JobRecord) -> TranscriptionRunRecord:
        payload = job.payload or {}
        run_id = payload.get("run_id") or f"txn_{job.id}"
        source_id = payload.get("source_id", "")
        media_path = payload.get("media_path", "")
        language = payload.get("language", "")

        self._mark(run_id, TranscriptionState.PROCESSING, source_id=source_id,
                   media_path=media_path)

        started = time.perf_counter()
        try:
            transcript = self.transcribe(media_path, language=language)
        except (ProviderUnavailable, PermanentError) as error:
            # A missing model or a bad key is not the media's fault and will
            # fail identically next time. Dead, and named so an operator can
            # see which of the two it was.
            self._fail(job, run_id, str(error), retry=False)
            return self._record(run_id)
        except (RetryableError, TranscriptionError, OSError) as error:
            self._fail(job, run_id, str(error), retry=True)
            return self._record(run_id)

        elapsed = time.perf_counter() - started
        record = self._succeed(run_id, transcript, elapsed, source_id)
        with self.database.unit_of_work(self.tenant_id) as uow:
            uow.jobs.succeed(
                job.id,
                {
                    "run_id": run_id,
                    "words": transcript.word_count,
                    "language": transcript.language,
                    "duration_s": transcript.duration_s,
                },
                self.clock(),
            )
        return record

    # -- the work ----------------------------------------------------------

    def transcribe(self, media_path: str, *, language: str = "") -> Transcript:
        """Transcribe one media file. No queue involved.

        Extracts audio, chunks it if it is long, transcribes each chunk, and
        stitches the results back into one timeline. The scratch directory is
        removed in a `finally`, including on the exception path — which is the
        path that matters, because a failure halfway through a three-hour
        podcast has hundreds of megabytes to answer for.
        """

        if not media_path or not os.path.exists(media_path):
            raise AudioExtractionFailed(f"no media at {media_path!r}")

        available = self.provider.availability()
        if not available.ready:
            raise ProviderUnavailable(
                f"{self.provider.info.name} cannot run: {available.detail}"
            )

        os.makedirs(self.config.workspace, exist_ok=True)
        directory = tempfile.mkdtemp(prefix="clipforge-audio-",
                                     dir=self.config.workspace)
        try:
            transcript = self._transcribe_in(directory, media_path, language)
        finally:
            if not self.config.keep_audio:
                shutil.rmtree(directory, ignore_errors=True)

        if transcript.word_count < self.config.min_words:
            # Segments but no words means captions with nothing to karaoke.
            # Permanent: the same provider on the same audio will do it again.
            raise PermanentError(
                f"{self.provider.info.name} returned "
                f"{transcript.word_count} words for "
                f"{os.path.basename(media_path)} — no usable transcript"
            )
        return transcript

    def _transcribe_in(
        self, directory: str, media_path: str, language: str
    ) -> Transcript:
        # One probe pass to decide the chunking. Cheap, and the alternative is
        # extracting the whole file to find out how long it is.
        duration_s = self._duration(media_path, directory)
        if self.audio.max_duration_s and duration_s > self.audio.max_duration_s:
            raise PermanentError(
                f"{os.path.basename(media_path)} is {duration_s / 3600:.1f}h, "
                f"over the {self.audio.max_duration_s / 3600:.1f}h ceiling"
            )

        windows = plan_chunks(duration_s, self.audio)
        if not windows:
            raise AudioExtractionFailed(
                f"{os.path.basename(media_path)} has no measurable duration"
            )

        results: list[tuple[AudioChunk, Transcript]] = []
        for index, (offset, length, keep_from, keep_to) in enumerate(windows):
            chunk = extract_chunk(media_path, directory, index, offset, length,
                                  keep_from, keep_to, self.audio)
            try:
                results.append(
                    (chunk, self.provider.transcribe(chunk.path, language=language))
                )
            finally:
                # Deleted as soon as it has been transcribed rather than at
                # the end. Peak disk stays one chunk, not the whole podcast.
                if not self.config.keep_audio:
                    with contextlib.suppress(OSError):
                        os.remove(chunk.path)
        return merge_chunks(results)

    def _duration(self, media_path: str, directory: str) -> float:
        from ..acquire.probe import MediaProber

        probe = MediaProber(ffmpeg=self.audio.ffmpeg or None).probe(media_path)
        if probe.duration_s and probe.duration_s > 0:
            if not probe.has_audio:
                raise AudioExtractionFailed(
                    f"{os.path.basename(media_path)} has no audio track — "
                    f"there is nothing to transcribe"
                )
            return probe.duration_s

        # Unmeasurable container. Extract once and read the WAV's own header,
        # which is exact for PCM.
        from .audio import extract_audio

        probe_path = os.path.join(directory, "probe.wav")
        extract_audio(media_path, probe_path, self.audio)
        duration = wav_duration_s(probe_path)
        with contextlib.suppress(OSError):
            os.remove(probe_path)
        return duration

    # -- persistence -------------------------------------------------------

    def _mark(
        self,
        run_id: str,
        state: TranscriptionState,
        *,
        source_id: str = "",
        media_path: str = "",
    ) -> None:
        with self.database.unit_of_work(self.tenant_id) as uow:
            record = uow.transcriptions.get(run_id) or TranscriptionRunRecord(
                id=run_id, tenant_id=self.tenant_id,
                source_id=source_id or None, media_path=media_path,
                provider=self.provider.info.name,
                model=self.provider.info.model,
            )
            record.state = state.value
            if state is TranscriptionState.PROCESSING:
                record.attempts += 1
                record.started_at = self.clock()
            uow.transcriptions.save(record)

    def _succeed(
        self, run_id: str, transcript: Transcript, elapsed_s: float, source_id: str
    ) -> TranscriptionRunRecord:
        with self.database.unit_of_work(self.tenant_id) as uow:
            record = uow.transcriptions.require(run_id)
            record.state = TranscriptionState.SUCCEEDED.value
            record.text = transcript.text
            record.transcript = transcript_to_dict(transcript)
            record.language = transcript.language
            record.language_confidence = transcript.language_confidence
            record.word_count = transcript.word_count
            record.segment_count = len(transcript.segments)
            record.mean_confidence = transcript.mean_confidence
            record.duration_s = transcript.duration_s
            record.elapsed_s = elapsed_s
            record.provider = (
                transcript.provider.name if transcript.provider else record.provider
            )
            record.model = (
                transcript.provider.model if transcript.provider else record.model
            )
            record.last_error = ""
            record.finished_at = self.clock()
            saved = uow.transcriptions.save(record)

            # The factory's clearance query reads `has_transcript` and knows
            # nothing about this layer. Setting it here is what makes
            # transcription visible to a pipeline that was written before it.
            if source_id and (source := uow.sources.get(source_id)):
                source.has_transcript = True
                uow.sources.save(source)
        return saved

    def _fail(self, job: JobRecord, run_id: str, message: str, *, retry: bool) -> None:
        now = self.clock()
        retry_at = None
        if retry:
            retry_at = now + timedelta(
                seconds=self.config.retry_base_s * (2 ** min(job.attempts, 5))
            )
        with self.database.unit_of_work(self.tenant_id) as uow:
            updated = uow.jobs.fail(job.id, message, retry_at, now)
            record = uow.transcriptions.get(run_id)
            if record is not None:
                # The queue decides whether this was the last attempt, so the
                # run's state follows the job's rather than guessing: a
                # retryable error on the final attempt is permanently failed.
                exhausted = updated.state == "dead"
                record.state = (
                    TranscriptionState.FAILED_PERMANENT.value
                    if (not retry or exhausted)
                    else TranscriptionState.FAILED_RETRYABLE.value
                )
                record.last_error = message
                record.finished_at = now if record.state.endswith("permanent") else None
                uow.transcriptions.save(record)

    def _fail_inline(self, run_id: str, message: str, *, permanent: bool) -> None:
        """Record a failure that had no job behind it."""

        now = self.clock()
        with self.database.unit_of_work(self.tenant_id) as uow:
            record = uow.transcriptions.get(run_id)
            if record is None:
                return
            record.state = (
                TranscriptionState.FAILED_PERMANENT.value if permanent
                else TranscriptionState.FAILED_RETRYABLE.value
            )
            record.last_error = message
            record.finished_at = now if permanent else None
            uow.transcriptions.save(record)

    def _record(self, run_id: str) -> TranscriptionRunRecord:
        with self.database.unit_of_work(self.tenant_id) as uow:
            return uow.transcriptions.require(run_id)

    # -- reading -----------------------------------------------------------

    def transcript_for(self, source_id: str) -> Transcript | None:
        """The stored transcript for a source, or None.

        Read from Postgres rather than recomputed, which is the point of
        persisting it: transcription is the most expensive stage in the
        pipeline and the only one whose output never changes for a given
        input.
        """

        with self.database.unit_of_work(self.tenant_id) as uow:
            record = uow.transcriptions.for_source(source_id)
        if record is None or record.state != TranscriptionState.SUCCEEDED.value:
            return None
        return transcript_from_dict(record.transcript or {})


# ---------------------------------------------------------------------------
# Serialisation
#
# Round-trippable, because the stored form is the only copy. A transcript that
# came back missing its word timings would silently produce captions with
# nothing to karaoke, and the transcription would have to be paid for again to
# find out why.
# ---------------------------------------------------------------------------


def transcript_to_dict(transcript: Transcript) -> dict[str, Any]:
    return transcript.to_dict()


def transcript_from_dict(payload: dict[str, Any]) -> Transcript:
    from .types import ProviderInfo

    words = tuple(_word(item) for item in payload.get("words") or [])
    segments = tuple(
        Segment(
            text=item.get("text", ""),
            start_s=float(item.get("start_s", 0.0) or 0.0),
            end_s=float(item.get("end_s", 0.0) or 0.0),
            words=tuple(_word(w) for w in item.get("words") or []),
            avg_logprob=item.get("avg_logprob"),
            no_speech_prob=item.get("no_speech_prob"),
        )
        for item in payload.get("segments") or []
    )
    raw_provider = payload.get("provider") or None
    provider = (
        ProviderInfo(
            name=raw_provider.get("name", ""),
            model=raw_provider.get("model", ""),
            version=raw_provider.get("version", ""),
            remote=bool(raw_provider.get("remote")),
            options=raw_provider.get("options") or {},
        )
        if raw_provider
        else None
    )
    return Transcript(
        text=payload.get("text", ""),
        segments=segments,
        words=words,
        language=payload.get("language", ""),
        language_confidence=payload.get("language_confidence"),
        duration_s=float(payload.get("duration_s", 0.0) or 0.0),
        provider=provider,
    )


def _word(item: dict[str, Any]) -> Word:
    return Word(
        text=item.get("text", ""),
        start_s=float(item.get("start_s", 0.0) or 0.0),
        end_s=float(item.get("end_s", 0.0) or 0.0),
        confidence=item.get("confidence"),
    )
