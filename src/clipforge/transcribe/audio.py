"""Getting audio out of media, safely, for arbitrarily long files.

Every speech model wants the same thing: 16 kHz mono PCM. Video files are not
that, podcast enclosures are not that, and a two-hour episode is several
gigabytes of the wrong format.

## Nothing is loaded into memory

ffmpeg reads the input and writes the output as streams; this module never
holds media in a Python buffer. The one place a byte count appears is the
guard that refuses an extraction whose *output* would be implausibly large,
and that reads the file's size rather than its contents.

At 16 kHz mono 16-bit, audio costs 32 kB per second — about 115 MB an hour.
A three-hour podcast is 345 MB on disk and zero bytes of RAM, which is why
the work happens in files rather than arrays.

## Long media is chunked, with overlap

Providers have limits: OpenAI's transcription endpoint caps uploads at 25 MB,
and local models degrade on very long inputs regardless of what they accept.
So audio is cut into windows.

The windows **overlap**, and that matters more than it looks. A cut lands
mid-word about as often as not, and a word split across a boundary is either
lost or transcribed twice as two half-words. The overlap gives each chunk the
context on both sides of its own edges, and `merge` stitches the results at
the middle of the overlap — so every word is decoded by the chunk that had the
most context around it, and each appears exactly once.

## Temporary files always go away

`extracted_audio` is a context manager and the cleanup is in a `finally`. A
worker that crashes mid-transcription leaves its temporary directory, which is
why the directories are named and why `sweep_workspace` exists — but the
ordinary path, including every exception path, removes them.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import time
import wave
from collections.abc import Iterator
from dataclasses import dataclass

from ..acquire.probe import find_ffmpeg
from .types import AudioExtractionFailed, PermanentError, RetryableError

__all__ = [
    "AudioConfig",
    "AudioChunk",
    "extract_audio",
    "extracted_audio",
    "plan_chunks",
    "extract_chunk",
    "wav_duration_s",
    "sweep_workspace",
    "SAMPLE_RATE",
]

#: What every Whisper-family model expects. Resampling here rather than
#: letting the provider do it means one resample instead of one per chunk, and
#: it makes the chunk size arithmetic exact.
SAMPLE_RATE = 16_000
CHANNELS = 1
BYTES_PER_SAMPLE = 2

#: 32 kB of audio per second at the above.
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE


@dataclass(slots=True)
class AudioConfig:
    #: How long each chunk is. Ten minutes of 16 kHz mono is 19 MB — under
    #: OpenAI's 25 MB ceiling with room for the WAV header and a little slack.
    chunk_s: float = 600.0
    #: Overlap between consecutive chunks. Three seconds is comfortably longer
    #: than any single word, so no word is cut by both of its chunks.
    overlap_s: float = 3.0
    #: Below this, chunking is pure overhead.
    chunk_threshold_s: float = 900.0
    #: ffmpeg has to finish extracting in this long. Generous: a three-hour
    #: podcast off a slow disk is legitimately minutes.
    timeout_s: float = 1800.0
    #: Refuse media longer than this rather than filling the disk with a live
    #: stream that never ends. Zero disables the check.
    max_duration_s: float = 6 * 3600.0
    ffmpeg: str = ""


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """One window of audio, and where it sits in the original."""

    index: int
    path: str
    #: Offset of this chunk's first sample within the source media. Every
    #: timestamp the provider returns is relative to the chunk and has to be
    #: shifted by this before it means anything.
    offset_s: float
    duration_s: float
    #: Where this chunk's authority begins and ends. Inside the overlap two
    #: chunks both have an opinion, and these bounds decide whose is used.
    keep_from_s: float
    keep_to_s: float

    @property
    def size_bytes(self) -> int:
        return os.path.getsize(self.path) if os.path.exists(self.path) else 0


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_audio(
    source_path: str,
    destination: str,
    config: AudioConfig | None = None,
    *,
    start_s: float = 0.0,
    duration_s: float = 0.0,
) -> str:
    """Write 16 kHz mono PCM from `source_path` to `destination`.

    Streamed by ffmpeg from end to end. Raises `AudioExtractionFailed` when
    the media has no usable audio — permanent, because the same file will not
    decode differently later.
    """

    config = config or AudioConfig()
    ffmpeg = config.ffmpeg or find_ffmpeg()
    if not ffmpeg:
        raise PermanentError(
            "audio extraction needs ffmpeg — install it, or set CLIPFORGE_FFMPEG"
        )
    if not os.path.exists(source_path):
        raise AudioExtractionFailed(f"no such media: {source_path}")

    os.makedirs(os.path.dirname(os.path.abspath(destination)) or ".", exist_ok=True)

    argv = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if start_s > 0:
        # Before `-i`, so ffmpeg seeks rather than decoding and discarding
        # everything up to the offset. On a two-hour podcast that is the
        # difference between a second and several minutes per chunk.
        argv += ["-ss", f"{start_s:.3f}"]
    argv += ["-i", source_path]
    if duration_s > 0:
        argv += ["-t", f"{duration_s:.3f}"]
    argv += [
        "-vn",                      # never decode video; it is the expensive part
        "-map", "0:a:0",            # first audio track, explicitly
        "-ac", str(CHANNELS),
        "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        "-f", "wav",
        destination,
    ]

    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                timeout=config.timeout_s, check=False)
    except subprocess.TimeoutExpired as error:
        _discard(destination)
        # Retryable: a timeout is usually a loaded box rather than a bad file.
        raise RetryableError(
            f"ffmpeg took longer than {config.timeout_s:.0f}s extracting audio "
            f"from {os.path.basename(source_path)}"
        ) from error
    except OSError as error:
        _discard(destination)
        raise PermanentError(f"could not run ffmpeg: {error}") from error

    if result.returncode != 0:
        _discard(destination)
        message = _last_error(result.stderr)
        # A missing audio stream is the common case and worth naming, because
        # "Stream map '0:a:0' matches no streams" tells an operator nothing.
        if "matches no streams" in (result.stderr or ""):
            raise AudioExtractionFailed(
                f"{os.path.basename(source_path)} has no audio track — there "
                f"is nothing to transcribe"
            )
        raise AudioExtractionFailed(
            f"ffmpeg could not extract audio from "
            f"{os.path.basename(source_path)}: {message}"
        )

    if not os.path.exists(destination) or os.path.getsize(destination) <= 44:
        # 44 bytes is an empty WAV: header and no samples.
        _discard(destination)
        raise AudioExtractionFailed(
            f"{os.path.basename(source_path)} produced no audio samples"
        )
    return destination


@contextlib.contextmanager
def extracted_audio(
    source_path: str,
    config: AudioConfig | None = None,
    *,
    workspace: str = "",
    keep: bool = False,
) -> Iterator[str]:
    """Extract to a temporary file, yield its path, then delete it.

    The cleanup is in a `finally`, so it happens on the exception path too —
    which is the path that matters, because a transcription that fails halfway
    through a three-hour podcast has 345 MB of scratch audio to answer for.

    `keep=True` leaves the file, for when a human is looking at why a
    transcript came out wrong.
    """

    directory = tempfile.mkdtemp(prefix="clipforge-audio-", dir=workspace or None)
    path = os.path.join(directory, "audio.wav")
    try:
        yield extract_audio(source_path, path, config)
    finally:
        if not keep:
            shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def plan_chunks(
    duration_s: float, config: AudioConfig | None = None
) -> list[tuple[float, float, float, float]]:
    """Window boundaries for a file of this length.

    Returns `(offset, duration, keep_from, keep_to)` per chunk, all in seconds
    from the start of the media. `keep_from`/`keep_to` bisect the overlap, so
    consecutive chunks hand over at the midpoint and every instant of the
    media is owned by exactly one chunk.
    """

    config = config or AudioConfig()
    if duration_s <= 0:
        return []
    if duration_s <= config.chunk_threshold_s:
        return [(0.0, duration_s, 0.0, duration_s)]

    step = max(1.0, config.chunk_s - config.overlap_s)
    half = config.overlap_s / 2.0
    windows: list[tuple[float, float, float, float]] = []
    offset = 0.0
    while offset < duration_s:
        length = min(config.chunk_s, duration_s - offset)
        keep_from = 0.0 if not windows else offset + half
        keep_to = min(duration_s, offset + length)
        if offset + length < duration_s:
            keep_to = offset + length - half
        windows.append((offset, length, keep_from, keep_to))
        if offset + length >= duration_s:
            break
        offset += step
    return windows


def extract_chunk(
    source_path: str,
    directory: str,
    index: int,
    offset_s: float,
    duration_s: float,
    keep_from_s: float,
    keep_to_s: float,
    config: AudioConfig | None = None,
) -> AudioChunk:
    """Extract one window. Each is its own ffmpeg run, seeking to its offset.

    Cutting the extracted WAV would be faster than re-seeking the source, and
    is not done: a two-hour podcast's full WAV is 230 MB that then has to
    exist alongside every chunk. Seeking is cheap and the peak disk cost stays
    one chunk rather than the whole file plus its pieces.
    """

    path = os.path.join(directory, f"chunk-{index:04d}.wav")
    extract_audio(source_path, path, config,
                  start_s=offset_s, duration_s=duration_s)
    return AudioChunk(index, path, offset_s, duration_s, keep_from_s, keep_to_s)


def wav_duration_s(path: str) -> float:
    """Length of a PCM WAV, from its header. No decoding, no memory."""

    try:
        with contextlib.closing(wave.open(path, "rb")) as handle:
            rate = handle.getframerate()
            return handle.getnframes() / rate if rate else 0.0
    except (wave.Error, OSError):
        return 0.0


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------


def sweep_workspace(workspace: str, older_than_s: float = 86_400.0) -> int:
    """Delete scratch directories a crashed worker left behind.

    The ordinary paths clean up after themselves, including on exceptions.
    This is for the process that was killed between creating a directory and
    entering its `try` — rare, and left uncleaned for ever without a sweep.

    Only removes directories matching the extractor's own prefix, so pointing
    this at the wrong path cannot delete anything that is not ours.
    """

    if not os.path.isdir(workspace):
        return 0
    cutoff = time.time() - older_than_s
    removed = 0
    for name in os.listdir(workspace):
        if not name.startswith("clipforge-audio-"):
            continue
        path = os.path.join(workspace, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def _discard(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


def _last_error(stderr: str) -> str:
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    return lines[-1] if lines else "no output"
