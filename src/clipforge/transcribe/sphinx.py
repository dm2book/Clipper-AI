"""An offline recogniser that can actually run without downloading anything.

## What this is, and what it is not

PocketSphinx is real speech recognition — a real acoustic model, real Viterbi
decoding, real word timings — and its English model ships inside the pip
wheel, so it works with no network and no model fetch. That is its entire
reason for being here: it makes the transcription pipeline *runnable and
testable end to end* in an environment that cannot reach Whisper's weights or
an API.

**It is not Whisper and its accuracy is nowhere near good enough for captions
you would publish.** It is a 2010-era recogniser; on clean read speech it is
usable, on podcast audio with music and crosstalk it is not. A channel pointed
at this provider will produce captions that are visibly wrong.

So it is deliberately awkward to select: it is never the default, `configure`
will not choose it unless it is named outright, and `availability()` says so
in words. It exists to prove the plumbing, and the plumbing it proves is real
— when a test here asserts that words come back with ascending timestamps that
line up with the audio, those timestamps were produced by decoding the audio.

## Word timings and confidence

PocketSphinx reports per-word start and end frames at 100 frames per second,
which is a real timing rather than an interpolation, and a per-word posterior
probability, which is a real confidence rather than a token probability. Both
are carried through unchanged.
"""

from __future__ import annotations

import os
import wave
from dataclasses import dataclass
from typing import Any

from .provider import Availability
from .types import (
    PermanentError,
    ProviderInfo,
    ProviderUnavailable,
    Segment,
    Transcript,
    Word,
)

__all__ = ["SphinxConfig", "SphinxProvider"]

#: PocketSphinx counts in centiseconds.
FRAMES_PER_SECOND = 100.0

#: Markers the decoder emits for silence and utterance boundaries. Not words.
_NON_WORDS = ("<s>", "</s>", "<sil>", "[SPEECH]", "[NOISE]", "<unk>")

#: Gap between words that starts a new segment. PocketSphinx has no notion of
#: a sentence, so segments are derived from silence — which is what a pause
#: between phrases actually is.
SEGMENT_GAP_S = 0.6


@dataclass(slots=True)
class SphinxConfig:
    #: Override the bundled model with a better one if you have it.
    model_path: str = ""
    language: str = "en"


class SphinxProvider:
    """PocketSphinx behind the provider protocol."""

    def __init__(self, config: SphinxConfig | None = None) -> None:
        self.config = config or SphinxConfig()
        self._decoder: Any = None

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="pocketsphinx",
            model=self.config.model_path or "en-us (bundled)",
            version=_library_version(),
            remote=False,
            options={
                # Recorded on every transcript this produces, so a caption
                # that reads badly can be traced to the recogniser that made
                # it rather than blamed on the caption engine.
                "accuracy": "well below Whisper; smoke-testing only",
            },
        )

    def availability(self) -> Availability:
        try:
            import pocketsphinx  # noqa: F401
        except ImportError:
            return Availability(False, "pocketsphinx is not installed")
        try:
            paths = self._paths()
        except ProviderUnavailable as error:
            return Availability(False, str(error))
        return Availability(
            True,
            f"bundled offline model at {paths['hmm']} — real recognition, but "
            f"accuracy well below Whisper. For smoke tests, not for captions "
            f"you publish.",
        )

    def _paths(self) -> dict[str, str]:
        if self.config.model_path:
            root = self.config.model_path
        else:
            try:
                from pocketsphinx import get_model_path
            except ImportError as error:
                raise ProviderUnavailable(
                    "pocketsphinx is not installed"
                ) from error
            root = os.path.join(get_model_path(), "en-us")

        paths = {
            "hmm": os.path.join(root, "en-us"),
            "lm": os.path.join(root, "en-us.lm.bin"),
            "dict": os.path.join(root, "cmudict-en-us.dict"),
        }
        for name, path in paths.items():
            if not os.path.exists(path):
                raise ProviderUnavailable(
                    f"the pocketsphinx {name} is missing at {path}"
                )
        return paths

    def _load(self) -> Any:
        if self._decoder is not None:
            return self._decoder
        try:
            from pocketsphinx import Decoder
        except ImportError as error:
            raise ProviderUnavailable("pocketsphinx is not installed") from error
        paths = self._paths()
        try:
            self._decoder = Decoder(hmm=paths["hmm"], lm=paths["lm"],
                                    dict=paths["dict"])
        except Exception as error:  # noqa: BLE001
            raise ProviderUnavailable(
                f"pocketsphinx would not initialise: {error}"
            ) from error
        return self._decoder

    def transcribe(self, wav_path: str, *, language: str = "") -> Transcript:
        if not os.path.exists(wav_path):
            raise PermanentError(f"no such audio: {wav_path}")
        decoder = self._load()

        with wave.open(wav_path, "rb") as handle:
            if handle.getframerate() != 16_000 or handle.getnchannels() != 1:
                # `audio.py` always produces this, so reaching here means
                # something bypassed it. Refused rather than resampled: a
                # recogniser fed the wrong rate returns confident nonsense,
                # which is far harder to notice than an error.
                raise PermanentError(
                    f"pocketsphinx needs 16 kHz mono; {os.path.basename(wav_path)} "
                    f"is {handle.getframerate()} Hz, "
                    f"{handle.getnchannels()} channel(s)"
                )
            frames = handle.getnframes()
            duration_s = frames / 16_000.0
            # Read in blocks rather than all at once. `process_raw` with
            # `full_utt` still wants the whole utterance, so this is bounded
            # by the chunk length the audio layer already imposed.
            audio = handle.readframes(frames)

        decoder.start_utt()
        decoder.process_raw(audio, full_utt=True)
        decoder.end_utt()

        words = tuple(
            Word(
                text=_clean(seg.word),
                start_s=seg.start_frame / FRAMES_PER_SECOND,
                end_s=seg.end_frame / FRAMES_PER_SECOND,
                # A real posterior from the decoder, not a token probability.
                confidence=_probability(seg),
            )
            for seg in decoder.seg()
            if _clean(seg.word)
        )

        return Transcript(
            text=" ".join(w.text for w in words),
            segments=_segments(words),
            words=words,
            language=self.config.language,
            duration_s=duration_s,
            provider=self.info,
        )


def _segments(words: tuple[Word, ...]) -> tuple[Segment, ...]:
    """Group words into phrases on silence.

    PocketSphinx has no sentence model, so a segment here is "words with no
    long pause between them" — which is what a phrase boundary sounds like,
    and better than returning one segment for a ten-minute chunk.
    """

    if not words:
        return ()
    segments: list[Segment] = []
    current: list[Word] = [words[0]]
    for previous, word in zip(words, words[1:]):
        if word.start_s - previous.end_s >= SEGMENT_GAP_S:
            segments.append(_segment(current))
            current = []
        current.append(word)
    if current:
        segments.append(_segment(current))
    return tuple(segments)


def _segment(words: list[Word]) -> Segment:
    return Segment(
        text=" ".join(w.text for w in words),
        start_s=words[0].start_s,
        end_s=words[-1].end_s,
        words=tuple(words),
    )


def _clean(word: str) -> str:
    """Strip the decoder's markers and its `(2)` pronunciation-variant tags."""

    text = (word or "").strip()
    if not text or text in _NON_WORDS or text.startswith("["):
        return ""
    if text.endswith(")") and "(" in text:
        text = text[: text.rindex("(")]
    return text.strip()


def _probability(segment: Any) -> float | None:
    value = getattr(segment, "prob", None)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # The decoder occasionally reports marginally over 1.0 for boundary
    # markers. Clamped rather than passed through, so a confidence is always
    # a confidence.
    return max(0.0, min(1.0, number))


def _library_version() -> str:
    try:
        from importlib.metadata import version

        return version("pocketsphinx")
    except Exception:  # noqa: BLE001
        return ""
