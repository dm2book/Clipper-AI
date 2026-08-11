"""Local Whisper-compatible inference, through faster-whisper.

## Status in this repository

**The code is real; it has not been run against a model here.** The
environment this was built in blocks the model host, so `WhisperModel` cannot
be constructed and no transcript has been produced by this adapter. What is
tested is everything around the model call — availability reporting, option
assembly, the mapping from faster-whisper's result objects onto `Transcript`,
and error classification — driven through an injected factory carrying objects
shaped exactly as faster-whisper returns them.

`availability()` reports `unverified=True` whenever it cannot prove the model
loads, so a deployment can tell the difference between "configured" and
"working" without reading this docstring.

## Why faster-whisper rather than openai-whisper

Same weights, CTranslate2 instead of PyTorch: roughly four times faster on CPU
and a fraction of the memory, which is the difference between transcription
being a background job and being a GPU bill. It also exposes `word_timestamps`
directly, where the reference implementation needs a separate alignment pass.

## What Whisper does and does not report

It gives per-word timestamps and a per-word `probability`, and per-segment
`avg_logprob` and `no_speech_prob`. It does **not** give a calibrated
confidence, and the word probability is not one — it is the model's own token
probability, useful for ranking and misleading as a percentage. It is carried
through unchanged and named for what it is.

`no_speech_prob` earns its place: Whisper hallucinates fluent text over
silence and music, which on a podcast with a musical intro produces a
confident transcript of words nobody said. A high value there is the signal
that a segment is invented, and the engine drops those rather than captioning
them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable

from .provider import Availability
from .types import (
    PermanentError,
    ProviderInfo,
    ProviderUnavailable,
    RetryableError,
    Segment,
    Transcript,
    Word,
)

__all__ = ["LocalWhisperConfig", "LocalWhisperProvider", "HALLUCINATION_NO_SPEECH"]

#: Segments above this `no_speech_prob` are treated as hallucination over
#: silence rather than speech. Whisper's own decoder uses 0.6 for its internal
#: fallback; this is deliberately stricter, because a caption of words nobody
#: said is worse than a gap.
HALLUCINATION_NO_SPEECH = 0.6


@dataclass(slots=True)
class LocalWhisperConfig:
    #: "tiny", "base", "small", "medium", "large-v3", or a path to a converted
    #: model directory. `small` is the usual production choice: `base` misses
    #: names and jargon, `medium` costs three times as much for a gain most
    #: viewers never see in a caption.
    model: str = "small"
    device: str = "cpu"            # "cpu", "cuda", "auto"
    compute_type: str = "int8"     # "int8" on CPU, "float16" on GPU
    beam_size: int = 5
    #: Voice-activity filtering. On by default: it is the cheapest defence
    #: against Whisper inventing text over the silence between speakers.
    vad_filter: bool = True
    language: str = ""             # "" detects
    #: Where converted models live. Set it in a container so a cold start does
    #: not re-download several gigabytes.
    download_root: str = ""
    cpu_threads: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class LocalWhisperProvider:
    """faster-whisper behind the provider protocol."""

    def __init__(
        self,
        config: LocalWhisperConfig | None = None,
        *,
        model_factory: Callable[[LocalWhisperConfig], Any] | None = None,
    ) -> None:
        self.config = config or LocalWhisperConfig()
        # Injected so the mapping and error handling are testable without a
        # model. The production path leaves it None and builds a real one.
        self._factory = model_factory
        self._model: Any = None

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="local-whisper",
            model=self.config.model,
            version=_library_version(),
            remote=False,
            options={
                "device": self.config.device,
                "compute_type": self.config.compute_type,
                "beam_size": self.config.beam_size,
                "vad_filter": self.config.vad_filter,
            },
        )

    # -- availability ------------------------------------------------------

    def availability(self) -> Availability:
        """Whether this can run, without downloading anything.

        Deliberately does not construct the model: doing so would download
        gigabytes as a side effect of a health check. So a positive answer
        here means "the library is installed and a model is named", and is
        reported as `unverified` unless the weights are already on disk.
        """

        if self._factory is not None:
            return Availability(True, "an injected model factory is in use")
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return Availability(
                False,
                "faster-whisper is not installed — `pip install faster-whisper`",
            )
        if self._model is not None:
            return Availability(True, f"{self.config.model} is loaded")

        cached = self._cached_model_path()
        if cached:
            return Availability(True, f"{self.config.model} is cached at {cached}")
        return Availability(
            True,
            f"faster-whisper is installed and configured for "
            f"'{self.config.model}', but the weights are not on disk and have "
            f"not been fetched — the first transcription will download them",
            unverified=True,
        )

    def _cached_model_path(self) -> str:
        root = (
            self.config.download_root
            or os.environ.get("HF_HOME")
            or os.path.expanduser("~/.cache/huggingface")
        )
        if os.path.isdir(self.config.model):
            return self.config.model
        candidate = os.path.join(
            root, "hub", f"models--Systran--faster-whisper-{self.config.model}"
        )
        return candidate if os.path.isdir(candidate) else ""

    # -- inference ---------------------------------------------------------

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        if self._factory is not None:
            self._model = self._factory(self.config)
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as error:
            raise ProviderUnavailable(
                "faster-whisper is not installed — `pip install faster-whisper`"
            ) from error

        kwargs: dict[str, Any] = {
            "device": self.config.device,
            "compute_type": self.config.compute_type,
        }
        if self.config.download_root:
            kwargs["download_root"] = self.config.download_root
        if self.config.cpu_threads:
            kwargs["cpu_threads"] = self.config.cpu_threads
        try:
            self._model = WhisperModel(self.config.model, **kwargs)
        except Exception as error:  # noqa: BLE001 - ctranslate2 raises broadly
            # Loading a model reaches the network on a cold start, so this is
            # as likely to be a proxy or a disk as a bad model name. Named
            # rather than retried: a download that 403s will 403 again.
            raise ProviderUnavailable(
                f"could not load Whisper model '{self.config.model}': {error}"
            ) from error
        return self._model

    def transcribe(self, wav_path: str, *, language: str = "") -> Transcript:
        if not os.path.exists(wav_path):
            raise PermanentError(f"no such audio: {wav_path}")
        model = self._load()

        options: dict[str, Any] = {
            "beam_size": self.config.beam_size,
            # The whole point of using this layer: without it there are no
            # word timings and the caption engine has nothing to work with.
            "word_timestamps": True,
            "vad_filter": self.config.vad_filter,
        }
        chosen = language or self.config.language
        if chosen:
            options["language"] = chosen
        options.update(self.config.extra)

        try:
            segments, info = model.transcribe(wav_path, **options)
            # faster-whisper returns a generator and does the work lazily, so
            # nothing has actually run until this list is built.
            materialised = list(segments)
        except Exception as error:  # noqa: BLE001 - the library raises broadly
            raise _classify(error) from error

        return _to_transcript(materialised, info, self.info)


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _to_transcript(segments: list[Any], info: Any, provider: ProviderInfo) -> Transcript:
    """faster-whisper's result objects onto `Transcript`.

    A plain function so it can be tested against recorded result shapes
    without a model — which is the only way it can be tested in an
    environment that cannot fetch weights.
    """

    words: list[Word] = []
    kept: list[Segment] = []
    texts: list[str] = []

    for raw in segments:
        no_speech = _get(raw, "no_speech_prob")
        if no_speech is not None and no_speech >= HALLUCINATION_NO_SPEECH:
            # Whisper writing fluent sentences over a musical intro. Dropped
            # rather than captioned: a confident transcript of words nobody
            # said is the failure people notice on a real feed.
            continue

        segment_words = tuple(
            Word(
                text=str(_get(w, "word", "")).strip(),
                start_s=float(_get(w, "start", 0.0) or 0.0),
                end_s=float(_get(w, "end", 0.0) or 0.0),
                # Whisper's token probability. Carried unchanged and *not*
                # rescaled into something that looks calibrated.
                confidence=_optional_float(_get(w, "probability")),
            )
            for w in (_get(raw, "words") or ())
            if str(_get(w, "word", "")).strip()
        )
        text = str(_get(raw, "text", "")).strip()
        kept.append(Segment(
            text=text,
            start_s=float(_get(raw, "start", 0.0) or 0.0),
            end_s=float(_get(raw, "end", 0.0) or 0.0),
            words=segment_words,
            avg_logprob=_optional_float(_get(raw, "avg_logprob")),
            no_speech_prob=_optional_float(no_speech),
        ))
        words.extend(segment_words)
        if text:
            texts.append(text)

    return Transcript(
        text=" ".join(texts).strip(),
        segments=tuple(kept),
        words=tuple(words),
        language=str(_get(info, "language", "") or ""),
        language_confidence=_optional_float(_get(info, "language_probability")),
        duration_s=float(_get(info, "duration", 0.0) or 0.0),
        provider=provider,
    )


def _classify(error: Exception) -> Exception:
    message = str(error).lower()
    if any(sign in message for sign in ("out of memory", "cuda", "cublas")):
        # A GPU that is busy now may not be in ten minutes.
        return RetryableError(f"whisper inference failed: {error}")
    if any(sign in message for sign in ("no such file", "invalid data", "format")):
        return PermanentError(f"whisper could not read the audio: {error}")
    return RetryableError(f"whisper inference failed: {error}")


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """faster-whisper returns named tuples; a recorded fixture is a dict."""

    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN is not a confidence


def _library_version() -> str:
    try:
        from importlib.metadata import version

        return version("faster-whisper")
    except Exception:  # noqa: BLE001
        return ""
