"""What is this file, and what does it look like?

Two jobs: measure the media (duration, geometry, codecs) and produce a
thumbnail.

## Measuring: boxes first, ffmpeg second

`mp4.read_mp4` handles the ISO family — MP4, M4A, M4V, MOV — which is what
YouTube and every podcast host actually serve. It is exact, needs no
subprocess, and works on a partially downloaded file.

ffmpeg is the fallback for everything else (WebM, Matroska, MP3, WAV) and the
cross-check when both are available. When the two disagree about duration by
more than a rounding error, the ffmpeg number wins and the disagreement is
recorded: it means the container header is lying, which happens with badly
remuxed files and matters, because a clip cut past the real end is a clip of
black.

## Thumbnails: three origins, cheapest first

1. **Embedded** — cover art already in the container. A read, no decoding.
   This is how a podcast episode gets artwork without ffmpeg existing.
2. **Remote** — the platform's own thumbnail URL, from yt-dlp metadata or a
   feed's `itunes:image`. A download, no decoding.
3. **Frame grab** — ffmpeg seeks into the video and writes one frame.

Only the third needs ffmpeg, and only the third applies to a plain video file
with no artwork. Ordering them this way means thumbnail extraction degrades to
"no frame grabs" rather than "no thumbnails" when ffmpeg is missing.

A frame grab does *not* take frame zero. The first frame of a talking-head
video is very often a black fade-in, and a wall of black thumbnails is a
product that looks broken.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass

from .mp4 import read_mp4
from .types import MediaProbe, ProbeFailed, Thumbnail

__all__ = ["ProbeConfig", "MediaProber", "find_ffmpeg", "find_ffprobe"]

#: Where in the clip to grab a frame, as a fraction of its duration. Far
#: enough in to be past a fade or a title card, not so far as to be a
#: mid-sentence blur.
FRAME_AT_FRACTION = 0.12
#: Floor and ceiling on that, in seconds.
MIN_FRAME_AT_S = 2.0
MAX_FRAME_AT_S = 60.0

#: Durations disagreeing by more than this are worth recording.
DURATION_TOLERANCE_S = 0.5

_ISO_SUFFIXES = frozenset({".mp4", ".m4v", ".m4a", ".mov"})


def find_ffmpeg() -> str:
    """ffmpeg, or "" if it is not installed.

    Checked at call time rather than import time: a container that installs
    ffmpeg in an entrypoint script would otherwise be told it is missing
    forever by a module imported at start-up.
    """

    return os.environ.get("CLIPFORGE_FFMPEG") or shutil.which("ffmpeg") or ""


def find_ffprobe() -> str:
    return os.environ.get("CLIPFORGE_FFPROBE") or shutil.which("ffprobe") or ""


@dataclass(slots=True)
class ProbeConfig:
    #: ffmpeg and ffprobe can hang on a malformed file. A worker blocked
    #: forever on one bad upload is a queue that stops.
    timeout_s: float = 60.0
    thumbnail_width: int = 640
    #: JPEG quality for a grabbed frame, on ffmpeg's 2 (best) to 31 scale.
    jpeg_quality: int = 4


class MediaProber:
    """Measures files and makes thumbnails."""

    def __init__(
        self,
        config: ProbeConfig | None = None,
        *,
        ffmpeg: str | None = None,
        ffprobe: str | None = None,
    ) -> None:
        self.config = config or ProbeConfig()
        self.ffmpeg = find_ffmpeg() if ffmpeg is None else ffmpeg
        self.ffprobe = find_ffprobe() if ffprobe is None else ffprobe

    # -- measuring ---------------------------------------------------------

    def probe(self, path: str) -> MediaProbe:
        if not os.path.exists(path):
            raise ProbeFailed(f"no such file: {path}")
        size = os.path.getsize(path)
        if size == 0:
            raise ProbeFailed(f"{path} is empty")

        boxes = self._probe_boxes(path, size)
        external = self._probe_ffmpeg(path)

        if boxes is None and external is None:
            raise ProbeFailed(
                f"{path}: not an ISO container and no ffmpeg/ffprobe available "
                f"to identify it"
            )
        if boxes is None:
            return external
        if external is None:
            return boxes

        # Both spoke. The container header is the faster answer but the more
        # frequently wrong one — a remux that copies `mvhd` from the source
        # leaves a duration describing a different file.
        prober = "mp4-boxes"
        duration = boxes.duration_s
        if (
            boxes.duration_s is not None
            and external.duration_s is not None
            and abs(boxes.duration_s - external.duration_s) > DURATION_TOLERANCE_S
        ):
            duration = external.duration_s
            prober = "ffmpeg(header-disagreed)"
        elif boxes.duration_s is None:
            duration = external.duration_s
            prober = "ffmpeg"

        return MediaProbe(
            duration_s=duration,
            width=boxes.width or external.width,
            height=boxes.height or external.height,
            fps=boxes.fps or external.fps,
            container=boxes.container or external.container,
            video_codec=boxes.video_codec or external.video_codec,
            audio_codec=boxes.audio_codec or external.audio_codec,
            has_audio=boxes.has_audio or external.has_audio,
            has_video=boxes.has_video or external.has_video,
            size_bytes=size,
            prober=prober,
            title=boxes.title or external.title,
            creator=boxes.creator or external.creator,
            created_at=boxes.created_at or external.created_at,
        )

    def _probe_boxes(self, path: str, size: int) -> MediaProbe | None:
        if os.path.splitext(path)[1].lower() not in _ISO_SUFFIXES:
            with open(path, "rb") as handle:
                from .mp4 import looks_like_mp4

                if not looks_like_mp4(handle.read(12)):
                    return None
        with open(path, "rb") as handle:
            info = read_mp4(handle, size)
        if info.duration_s is None and not info.has_audio and not info.has_video:
            return None
        return MediaProbe(
            duration_s=info.duration_s,
            width=info.width,
            height=info.height,
            fps=info.fps,
            container=info.brand,
            video_codec=info.video_codec,
            audio_codec=info.audio_codec,
            has_audio=info.has_audio,
            has_video=info.has_video,
            size_bytes=size,
            prober="mp4-boxes",
            title=info.title,
            creator=info.creator,
            created_at=info.created_at,
        )

    def _probe_ffmpeg(self, path: str) -> MediaProbe | None:
        if self.ffprobe:
            return self._probe_with_ffprobe(path)
        if self.ffmpeg:
            return self._probe_with_ffmpeg(path)
        return None

    def _probe_with_ffprobe(self, path: str) -> MediaProbe | None:
        result = self._run([
            self.ffprobe, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", path,
        ])
        if result is None or not result.stdout.strip():
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return _from_ffprobe(payload, os.path.getsize(path))

    def _probe_with_ffmpeg(self, path: str) -> MediaProbe | None:
        """Read what ffmpeg prints to stderr when asked to decode nothing.

        Text parsing, and it is the second choice for that reason. It exists
        because ffprobe ships separately and plenty of static builds omit it —
        and a duration extractor that silently returns "unknown" because a
        binary is missing is one that works in development and reports
        zero-length videos in production.
        """

        result = self._run([self.ffmpeg, "-hide_banner", "-i", path], expect_fail=True)
        if result is None:
            return None
        return _from_ffmpeg_stderr(result.stderr, os.path.getsize(path))

    # -- thumbnails --------------------------------------------------------

    def thumbnail(
        self,
        path: str,
        destination: str,
        probe: MediaProbe | None = None,
        *,
        embedded_first: bool = True,
    ) -> Thumbnail | None:
        """A thumbnail for this file, or None if one cannot be made.

        None rather than a placeholder image. A grey rectangle stored as a
        real thumbnail is indistinguishable downstream from a real one, and
        the operator finds out when a hundred of them reach a feed.
        """

        os.makedirs(os.path.dirname(os.path.abspath(destination)) or ".",
                    exist_ok=True)

        if embedded_first:
            if found := self._embedded(path, destination):
                return found
        return self._frame(path, destination, probe)

    def _embedded(self, path: str, destination: str) -> Thumbnail | None:
        """Cover art already in the container. No decoding, no ffmpeg."""

        if os.path.splitext(path)[1].lower() not in _ISO_SUFFIXES:
            return None
        try:
            with open(path, "rb") as handle:
                info = read_mp4(handle)
        except OSError:
            return None
        if not info.cover_art:
            return None
        target = _with_suffix(destination, ".png" if info.cover_type == "png" else ".jpg")
        with open(target, "wb") as handle:
            handle.write(info.cover_art)
        return Thumbnail(path=target, origin="embedded")

    def _frame(
        self, path: str, destination: str, probe: MediaProbe | None
    ) -> Thumbnail | None:
        if not self.ffmpeg:
            return None
        probe = probe or self.probe(path)
        if not probe.has_video:
            # Nothing to grab. An audio file with no artwork simply has no
            # thumbnail, and inventing a waveform picture would be inventing
            # content.
            return None

        at = _frame_position(probe.duration_s)
        target = _with_suffix(destination, ".jpg")
        result = self._run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            # `-ss` before `-i` seeks by keyframe, which is fast and is why a
            # thumbnail for a two-hour podcast does not take two minutes.
            "-ss", f"{at:.3f}", "-i", path,
            "-frames:v", "1",
            "-vf", f"scale={self.config.thumbnail_width}:-2",
            "-q:v", str(self.config.jpeg_quality),
            target,
        ])
        if result is None or result.returncode != 0 or not os.path.exists(target):
            # A seek past the real end produces no frame. Retry at the very
            # start rather than giving up: a thumbnail from frame zero beats
            # no thumbnail, even if it is sometimes a fade-in.
            result = self._run([
                self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", path, "-frames:v", "1",
                "-vf", f"scale={self.config.thumbnail_width}:-2",
                "-q:v", str(self.config.jpeg_quality), target,
            ])
            at = 0.0
            if result is None or result.returncode != 0 or not os.path.exists(target):
                return None

        width, height = _jpeg_size(target)
        return Thumbnail(path=target, width=width, height=height,
                         origin="frame", at_s=at)

    # -- subprocess --------------------------------------------------------

    def _run(
        self, argv: list[str], *, expect_fail: bool = False
    ) -> subprocess.CompletedProcess | None:
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True,
                timeout=self.config.timeout_s, check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            # A hung ffmpeg is a worker that stops. Losing the metadata for
            # one file is the cheaper failure.
            return None
        if result.returncode != 0 and not expect_fail:
            return result
        return result


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _frame_position(duration_s: float | None) -> float:
    if not duration_s or duration_s <= 0:
        return MIN_FRAME_AT_S
    at = duration_s * FRAME_AT_FRACTION
    at = max(MIN_FRAME_AT_S, min(MAX_FRAME_AT_S, at))
    # Never seek past the end; leave a little room for a short clip.
    return min(at, max(0.0, duration_s - 0.5))


def _with_suffix(path: str, suffix: str) -> str:
    root, existing = os.path.splitext(path)
    return path if existing.lower() == suffix else root + suffix


def _from_ffprobe(payload: dict, size: int) -> MediaProbe:
    fmt = payload.get("format", {}) or {}
    streams = payload.get("streams", []) or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}

    # An attached picture is artwork, not footage.
    if video is not None and (video.get("disposition") or {}).get("attached_pic"):
        video = None

    return MediaProbe(
        duration_s=_float_or_none(fmt.get("duration")),
        width=video.get("width") if video else None,
        height=video.get("height") if video else None,
        fps=_ratio(video.get("avg_frame_rate")) if video else None,
        container=(fmt.get("format_name") or "").split(",")[0],
        video_codec=video.get("codec_name", "") if video else "",
        audio_codec=audio.get("codec_name", "") if audio else "",
        has_audio=audio is not None,
        has_video=video is not None,
        size_bytes=size,
        prober="ffprobe",
        title=tags.get("title", ""),
        creator=tags.get("artist", "") or tags.get("author", ""),
    )


def _from_ffmpeg_stderr(text: str, size: int) -> MediaProbe | None:
    """Pull duration and stream facts out of ffmpeg's banner."""

    duration: float | None = None
    width = height = None
    fps = None
    video_codec = audio_codec = ""
    has_video = has_audio = False
    container = ""

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Input #0"):
            _, _, tail = stripped.partition(", ")
            container = tail.split(",")[0].strip()
        elif stripped.startswith("Duration:"):
            value = stripped.split("Duration:", 1)[1].split(",")[0].strip()
            duration = _hhmmss(value)
        elif "Video:" in stripped and stripped.startswith("Stream"):
            if "attached pic" in stripped:
                continue  # artwork, not footage
            has_video = True
            after = stripped.split("Video:", 1)[1].strip()
            video_codec = after.split()[0].strip(",")
            for token in after.split(","):
                token = token.strip()
                if "x" in token and token.split("x")[0].strip().isdigit():
                    geometry = token.split()[0]
                    left, _, right = geometry.partition("x")
                    if left.isdigit() and right.isdigit():
                        width, height = int(left), int(right)
                if token.endswith(" fps"):
                    fps = _float_or_none(token[:-4])
        elif "Audio:" in stripped and stripped.startswith("Stream"):
            has_audio = True
            audio_codec = stripped.split("Audio:", 1)[1].strip().split()[0].strip(",")

    if duration is None and not has_audio and not has_video:
        return None
    return MediaProbe(
        duration_s=duration, width=width, height=height, fps=fps,
        container=container, video_codec=video_codec, audio_codec=audio_codec,
        has_audio=has_audio, has_video=has_video, size_bytes=size,
        prober="ffmpeg",
    )


def _hhmmss(value: str) -> float | None:
    if value.upper().startswith("N/A"):
        return None
    parts = value.split(":")
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return None
    total = 0.0
    for number in numbers:
        total = total * 60 + number
    return total


def _float_or_none(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # NaN is not a duration


def _ratio(value: str | None) -> float | None:
    if not value or "/" not in value:
        return _float_or_none(value)
    numerator, _, denominator = value.partition("/")
    try:
        top, bottom = float(numerator), float(denominator)
    except ValueError:
        return None
    return round(top / bottom, 3) if bottom else None


def _jpeg_size(path: str) -> tuple[int, int]:
    """Width and height from JPEG SOF markers, without an imaging library."""

    try:
        with open(path, "rb") as handle:
            data = handle.read(1 << 16)
    except OSError:
        return 0, 0
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return width, height
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if length <= 0:
            break
        index += 2 + length
    return 0, 0
