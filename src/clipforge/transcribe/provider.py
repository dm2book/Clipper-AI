"""The provider contract, and how chunk results are stitched together.

One method — `transcribe(path) -> Transcript` — over a 16 kHz mono WAV that
`audio.py` has already produced. Providers do not see video, do not resample,
and do not chunk: that work is identical for all of them and doing it once
means a new provider is an adapter rather than a pipeline.

A provider is also asked whether it can run *before* it is used
(`availability`), because "no model on disk" and "the model produced nothing"
need different answers, and finding out which at 3am from a stack trace is
avoidable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .types import ProviderInfo, Transcript, Word

__all__ = ["Availability", "TranscriptionProvider", "merge_chunks"]


@dataclass(frozen=True, slots=True)
class Availability:
    """Whether a provider can run, and if not, what is missing."""

    ready: bool
    detail: str = ""
    #: True when the check could not be made without a network call or a model
    #: download — so `ready` is a claim about configuration, not about the
    #: service actually answering. Reported rather than assumed, because "the
    #: key is set" and "the key works" are different facts.
    unverified: bool = False

    def to_dict(self) -> dict[str, object]:
        return {"ready": self.ready, "detail": self.detail,
                "unverified": self.unverified}


@runtime_checkable
class TranscriptionProvider(Protocol):
    """Speech to text with timings.

    Implementations must return times in seconds from the start of the audio
    they were handed, and must not invent confidence. `None` is the correct
    answer when a model does not report one.
    """

    @property
    def info(self) -> ProviderInfo: ...

    def availability(self) -> Availability:
        """Can this run? Called before work is queued, not after it fails."""

    def transcribe(self, wav_path: str, *, language: str = "") -> Transcript:
        """Transcribe one 16 kHz mono WAV.

        `language` is a hint, not a demand: passing one skips detection and is
        faster, but a provider that detects a different language should say so
        in the result rather than forcing the hint.
        """


# ---------------------------------------------------------------------------
# Stitching
# ---------------------------------------------------------------------------


def merge_chunks(results: list[tuple[object, Transcript]]) -> Transcript:
    """Combine per-chunk transcripts into one.

    Two things happen here and both matter.

    **Timestamps are shifted.** A provider times everything from the start of
    the audio it saw. A word at 12s of chunk four is not at 12s of the
    podcast, and a caption built from unshifted times drifts further out of
    sync with every chunk.

    **The overlap is resolved.** Consecutive chunks share a few seconds, so a
    word near a boundary appears in both. Each chunk owns the span between its
    `keep_from_s` and `keep_to_s` — the midpoint of the overlap — and words
    outside that span are dropped. Keeping both copies would duplicate words
    in the caption; keeping neither would lose the word that the cut landed
    in the middle of, which is why the chunks overlap at all.

    Ownership is decided on a word's *midpoint* rather than its start, so a
    word straddling the handover lands in exactly one chunk however the two
    providers disagree about where it began.
    """

    if not results:
        return Transcript(text="")
    if len(results) == 1:
        chunk, transcript = results[0]
        offset = getattr(chunk, "offset_s", 0.0)
        return _shift(transcript, offset) if offset else transcript

    words: list[Word] = []
    segments = []
    languages: dict[str, float] = {}
    provider = None
    end_s = 0.0

    for chunk, transcript in results:
        offset = getattr(chunk, "offset_s", 0.0)
        keep_from = getattr(chunk, "keep_from_s", float("-inf"))
        keep_to = getattr(chunk, "keep_to_s", float("inf"))
        provider = provider or transcript.provider

        if transcript.language:
            # Weighted by how much audio the chunk covered: a two-word chunk
            # of a Spanish advert should not outvote an hour of English.
            languages[transcript.language] = (
                languages.get(transcript.language, 0.0)
                + getattr(chunk, "duration_s", 1.0)
            )

        for word in transcript.words:
            shifted = word.shifted(offset)
            middle = (shifted.start_s + shifted.end_s) / 2.0
            if keep_from <= middle < keep_to:
                words.append(shifted)
                end_s = max(end_s, shifted.end_s)

        for segment in transcript.segments:
            shifted = segment.shifted(offset)
            middle = (shifted.start_s + shifted.end_s) / 2.0
            if keep_from <= middle < keep_to:
                segments.append(shifted)
                end_s = max(end_s, shifted.end_s)

    words.sort(key=lambda w: (w.start_s, w.end_s))
    segments.sort(key=lambda s: (s.start_s, s.end_s))

    # Rebuilt from the kept segments rather than concatenating each chunk's
    # own `text`, which would reintroduce every duplicate the overlap
    # resolution just removed.
    text = " ".join(s.text.strip() for s in segments if s.text.strip())
    if not text:
        text = " ".join(w.text for w in words)

    language = max(languages, key=languages.get) if languages else ""
    return Transcript(
        text=text.strip(),
        segments=tuple(segments),
        words=tuple(words),
        language=language,
        duration_s=end_s,
        provider=provider,
    )


def _shift(transcript: Transcript, offset: float) -> Transcript:
    return Transcript(
        text=transcript.text,
        segments=tuple(s.shifted(offset) for s in transcript.segments),
        words=tuple(w.shifted(offset) for w in transcript.words),
        language=transcript.language,
        language_confidence=transcript.language_confidence,
        duration_s=transcript.duration_s + offset,
        provider=transcript.provider,
    )
