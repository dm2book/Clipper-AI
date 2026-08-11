"""What transcription produces, and the states it moves through.

The output contract is deliberately richer than "the text". Everything
downstream needs a different part of it:

* the **caption engine** refuses sentence-level subtitles outright and needs
  word-level timings, because there is nothing to karaoke and nowhere precise
  to place an emoji without them;
* the **viral detector** scores segments, and a segment boundary that does not
  line up with a sentence produces clips that start mid-word;
* the **hook generator** reads the text;
* the **clip gate** needs the detected language, because a channel that
  publishes English will otherwise cheerfully clip a German podcast.

Confidence is optional throughout and that is not laziness. Whisper does not
report per-word confidence in its public API — it reports an average log
probability per segment — and inventing a number that looks like a probability
is worse than admitting there is not one. `None` means "this provider does not
say"; it never means "zero".
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..publish.types import utcnow

__all__ = [
    "Word",
    "Segment",
    "Transcript",
    "TranscriptionState",
    "ProviderInfo",
    "TranscriptionError",
    "RetryableError",
    "PermanentError",
    "AudioExtractionFailed",
    "ProviderUnavailable",
]


# ---------------------------------------------------------------------------
# Errors
#
# Split by what the queue should do, exactly as in the acquisition layer. A
# retry policy that has to read message text retries a bad API key forever and
# gives up on a rate limit immediately.
# ---------------------------------------------------------------------------


class TranscriptionError(Exception):
    """Base for everything this package raises."""


class RetryableError(TranscriptionError):
    """Worth another pass: a timeout, a 5xx, a rate limit, a busy GPU."""


class PermanentError(TranscriptionError):
    """Not worth another pass: a bad key, an unsupported format, silence."""


class AudioExtractionFailed(PermanentError):
    """ffmpeg could not get audio out of the file.

    Permanent because the same file will not decode differently in ten
    minutes. Usually it means the media has no audio track at all, which the
    acquisition layer should have caught — but a file can also be truncated
    after acquisition, so it is checked again here.
    """


class ProviderUnavailable(PermanentError):
    """The configured provider cannot run at all.

    A missing model, an unset API key, a library that is not installed. Named
    separately because it is an operator's configuration problem rather than a
    problem with the media, and retrying it just means failing identically
    once a minute until someone looks.
    """


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------


class TranscriptionState(str, enum.Enum):
    """Where a transcription job is.

    The two failure states are distinct rather than one `failed` with a flag,
    because the difference decides what happens next and a flag is easy to
    forget to read. `FAILED_RETRYABLE` goes back on the queue with backoff;
    `FAILED_PERMANENT` is dead and needs a person.
    """

    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_PERMANENT = "failed_permanent"

    @property
    def failed(self) -> bool:
        return self in (self.FAILED_RETRYABLE, self.FAILED_PERMANENT)

    @property
    def terminal(self) -> bool:
        """No further work will happen without someone intervening."""

        return self in (self.SUCCEEDED, self.FAILED_PERMANENT)


# ---------------------------------------------------------------------------
# The transcript
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Word:
    """One word, with the times it was actually spoken.

    Times are seconds from the start of the *media*, not of the chunk the
    provider saw. Chunk offsets are applied when results are merged, because a
    word timed relative to a chunk is a caption that drifts further out of
    sync with every minute of a podcast.
    """

    text: str
    start_s: float
    end_s: float
    #: 0..1 where the provider reports one, `None` where it does not. Never
    #: invented: a fabricated confidence is worse than an absent one, because
    #: something downstream will eventually filter on it.
    confidence: float | None = None

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    def shifted(self, offset_s: float) -> Word:
        return Word(self.text, self.start_s + offset_s, self.end_s + offset_s,
                    self.confidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "start_s": round(self.start_s, 3),
            "end_s": round(self.end_s, 3),
            "confidence": (
                round(self.confidence, 4) if self.confidence is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class Segment:
    """A phrase or sentence, as the provider grouped it.

    Kept alongside the words rather than derived from them. Whisper's segment
    boundaries come from its own decoding and land on real pauses; re-deriving
    them from word gaps throws away information the model had and this layer
    does not.
    """

    text: str
    start_s: float
    end_s: float
    words: tuple[Word, ...] = ()
    #: Whisper's `avg_logprob` — a log probability, not a 0..1 score. Kept in
    #: its own units rather than squashed into a fake probability.
    avg_logprob: float | None = None
    #: Whisper's `no_speech_prob`. High values mark hallucinated text over
    #: silence, which is the single most common Whisper failure on podcasts
    #: with long musical intros.
    no_speech_prob: float | None = None

    def shifted(self, offset_s: float) -> Segment:
        return Segment(
            self.text, self.start_s + offset_s, self.end_s + offset_s,
            tuple(w.shifted(offset_s) for w in self.words),
            self.avg_logprob, self.no_speech_prob,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "start_s": round(self.start_s, 3),
            "end_s": round(self.end_s, 3),
            "words": [w.to_dict() for w in self.words],
            "avg_logprob": self.avg_logprob,
            "no_speech_prob": self.no_speech_prob,
        }


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    """Which engine produced a transcript, and how.

    Persisted with every result. Two transcripts from different models are not
    comparable, and a retrained or upgraded provider changes caption quality —
    so "which model said this?" has to be answerable a year later, when the
    question is why last spring's clips read better.
    """

    name: str
    model: str = ""
    version: str = ""
    #: True when the transcript came from a network service rather than local
    #: inference. Decides whether a failure is worth retrying and whether the
    #: media left the building.
    remote: bool = False
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "model": self.model, "version": self.version,
            "remote": self.remote, "options": self.options,
        }


@dataclass(frozen=True, slots=True)
class Transcript:
    """Everything one transcription produced."""

    text: str
    segments: tuple[Segment, ...] = ()
    words: tuple[Word, ...] = ()
    #: ISO 639-1 where the provider detects one. Empty means it did not say —
    #: which is different from English, and the clip gate treats it that way.
    language: str = ""
    #: The provider's own confidence in the language, where reported.
    language_confidence: float | None = None
    duration_s: float = 0.0
    provider: ProviderInfo | None = None
    created_at: datetime = field(default_factory=utcnow)

    @property
    def word_count(self) -> int:
        return len(self.words)

    @property
    def has_word_timings(self) -> bool:
        """Whether captions can be built from this.

        The caption engine needs word-level timings and refuses to invent
        them. A transcript without them is still useful for hooks and topic
        detection, so this is a question a caller asks rather than a failure.
        """

        return bool(self.words)

    @property
    def mean_confidence(self) -> float | None:
        """Mean over words that reported one, or None if none did."""

        scored = [w.confidence for w in self.words if w.confidence is not None]
        return sum(scored) / len(scored) if scored else None

    def between(self, start_s: float, end_s: float) -> tuple[Word, ...]:
        """The words inside a window — how a clip gets its own transcript."""

        return tuple(
            w for w in self.words if w.start_s >= start_s and w.end_s <= end_s
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "language": self.language,
            "language_confidence": self.language_confidence,
            "duration_s": round(self.duration_s, 3),
            "word_count": self.word_count,
            "has_word_timings": self.has_word_timings,
            "mean_confidence": self.mean_confidence,
            "provider": self.provider.to_dict() if self.provider else None,
            "segments": [s.to_dict() for s in self.segments],
            "words": [w.to_dict() for w in self.words],
        }
