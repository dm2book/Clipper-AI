"""What acquisition works with: references, downloads, probes, results.

The layer's job is to turn *something a user typed* — a URL, a feed address, a
path to a file they uploaded — into rights-annotated material the factory can
clip, with the bytes on disk and the metadata in Postgres.

Two things it deliberately does not do:

* **It does not decide whether material may be published.** That is
  `factory.sources.Rights`, and acquisition never sets a basis better than
  `UNVERIFIED` on its own. Downloading something is not the same as being
  allowed to republish it, and a layer that conflated the two would launder a
  rights decision into a technical one.
* **It does not invent metadata.** A duration that could not be measured is
  `None`, not zero. Zero is a number the clip detector will happily divide by.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..publish.types import utcnow

__all__ = [
    "InputKind",
    "SourceRef",
    "DownloadState",
    "Download",
    "MediaProbe",
    "Thumbnail",
    "Acquisition",
    "AcquisitionError",
    "UnsupportedInput",
    "DownloadFailed",
    "PermanentDownloadFailed",
    "ProbeFailed",
    "RetryableError",
    "PermanentError",
]


class InputKind(str, enum.Enum):
    """What the operator handed us."""

    YOUTUBE_VIDEO = "youtube_video"
    YOUTUBE_CHANNEL = "youtube_channel"
    PODCAST_FEED = "podcast_feed"
    #: A file already on disk — the uploaded-MP4 path.
    LOCAL_FILE = "local_file"
    #: A plain http(s) URL to a media file. Podcast enclosures resolve to this.
    MEDIA_URL = "media_url"


# ---------------------------------------------------------------------------
# Errors
#
# Split by *what a caller should do*, not by where they came from. A retry
# policy that has to inspect message text is a retry policy that hammers a 404
# eight times and gives up on a 503 immediately.
# ---------------------------------------------------------------------------


class AcquisitionError(Exception):
    """Base for everything this package raises."""


class RetryableError(AcquisitionError):
    """Worth trying again: a timeout, a 5xx, a dropped connection."""


class PermanentError(AcquisitionError):
    """Not worth trying again: a 404, a private video, a malformed feed.

    Retrying these is how a queue spends its afternoon on a video that was
    deleted last week.
    """


class UnsupportedInput(PermanentError):
    """Nothing here recognises this input."""


class DownloadFailed(AcquisitionError):
    """The bytes did not arrive intact, and the attempts are spent.

    Retryable by the *queue* even though the downloader has stopped: its
    attempt budget is per pass, and a CDN that was unwell for ninety seconds
    is worth another pass in ten minutes. The `.part` file stays on disk, so
    that pass resumes rather than starting over.
    """


class PermanentDownloadFailed(DownloadFailed, PermanentError):
    """The bytes will never arrive: a 404, a 403, a file over the ceiling.

    Both a `DownloadFailed` (so a caller handling download errors catches it)
    and a `PermanentError` (so the queue dead-letters it instead of spending
    the afternoon on a video that was deleted last week). Inheriting from one
    only is how a 404 ends up being retried eight times with backoff — which
    is the bug this class exists to make impossible.
    """


class ProbeFailed(AcquisitionError):
    """The file arrived but could not be understood as media."""


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceRef:
    """A parsed, normalised reference to one thing that can be acquired.

    Normalised matters: `youtu.be/X`, `youtube.com/watch?v=X` and
    `youtube.com/watch?v=X&t=90s&list=PL...` are the same video, and a system
    that treats them as three will download it three times and clip it three
    times onto the same channel.
    """

    kind: InputKind
    #: The canonical form. For YouTube, the bare video or channel id; for a
    #: feed or media URL, the URL with tracking parameters stripped; for a
    #: local file, the absolute path.
    key: str
    #: What the user actually typed, kept for the audit trail.
    raw: str = ""
    url: str = ""
    #: Anything the resolver learned on the way — a playlist id it discarded,
    #: a start offset it noticed.
    hints: dict[str, Any] = field(default_factory=dict)

    @property
    def is_container(self) -> bool:
        """True when this expands into many sources rather than being one.

        A channel and a feed are containers; a video and a file are not.
        """

        return self.kind in (InputKind.YOUTUBE_CHANNEL, InputKind.PODCAST_FEED)


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


class DownloadState(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    #: Bytes are on disk but incomplete — the resumable case. Distinct from
    #: FAILED because it is not an error, it is a download waiting for its
    #: next attempt to pick up where the last one stopped.
    PARTIAL = "partial"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class Download:
    """One file being fetched, and enough state to resume it.

    Resumption needs three things and all three are here: how many bytes are
    already on disk, and a validator (`etag` or `last_modified`) proving the
    remote file is still the one those bytes came from. Resuming without a
    validator is how a re-encoded upload produces a file that is the first
    half of one video and the second half of another — which passes a size
    check and fails to decode, hours later, in the renderer.
    """

    download_id: str
    url: str
    #: Where the completed file lands. Bytes accumulate in `path + ".part"`.
    path: str
    state: DownloadState = DownloadState.QUEUED
    bytes_done: int = 0
    #: None when the server sent no Content-Length, which is legal and common
    #: for chunked responses. Progress is unknowable then, not zero.
    bytes_total: int | None = None
    etag: str = ""
    last_modified: str = ""
    content_type: str = ""
    #: Set once the file is complete and hashed. The identity of the bytes.
    checksum: str = ""
    attempts: int = 0
    last_error: str = ""
    #: True when the server answered a Range request with 206. A server that
    #: ignores Range and returns 200 is not resumable, and pretending
    #: otherwise appends a second copy of the file to the first.
    resumable: bool = False
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None

    @property
    def part_path(self) -> str:
        return f"{self.path}.part"

    @property
    def progress(self) -> float | None:
        """0..1, or None when the total is unknown."""

        if not self.bytes_total:
            return None
        return min(1.0, self.bytes_done / self.bytes_total)

    @property
    def validator(self) -> str:
        """The strongest thing we have to prove the remote file is unchanged."""

        return self.etag or self.last_modified

    def to_dict(self) -> dict[str, Any]:
        return {
            "download_id": self.download_id,
            "url": self.url,
            "state": self.state.value,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "progress": self.progress,
            "resumable": self.resumable,
            "attempts": self.attempts,
            "checksum": self.checksum,
            "last_error": self.last_error,
        }


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MediaProbe:
    """What the file turned out to be.

    Every field is optional because every field genuinely can be unknown, and
    the alternative — zero — is a number that flows downstream and gets
    divided by. A clip detector handed `duration_s=0` produces no clips and no
    error; handed `None` it can say why.
    """

    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    #: Container brand: "isom", "mp42", "matroska". Useful in a bug report.
    container: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    has_audio: bool = False
    has_video: bool = False
    size_bytes: int = 0
    #: Where the numbers came from — "mp4-boxes" or "ffmpeg". A duration that
    #: disagrees between the two is worth knowing about.
    prober: str = ""
    title: str = ""
    creator: str = ""
    created_at: datetime | None = None

    @property
    def usable(self) -> bool:
        """Enough to clip: a known duration and an audio track.

        Video is not required — a podcast is audio over a gameplay bed, and
        refusing audio-only material would refuse a whole product line.
        """

        return bool(self.duration_s and self.duration_s > 0 and self.has_audio)

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_s": self.duration_s,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "container": self.container,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "has_audio": self.has_audio,
            "has_video": self.has_video,
            "size_bytes": self.size_bytes,
            "prober": self.prober,
            "usable": self.usable,
        }


@dataclass(frozen=True, slots=True)
class Thumbnail:
    path: str
    width: int = 0
    height: int = 0
    #: "embedded" (cover art in the container), "frame" (grabbed by ffmpeg),
    #: or "remote" (the platform's own thumbnail, downloaded).
    origin: str = ""
    #: Only set for a frame grab.
    at_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "origin": self.origin,
            "at_s": self.at_s,
        }


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Acquisition:
    """One acquired source: the reference, the bytes, and what they are."""

    acquisition_id: str
    ref: SourceRef
    #: Populated once the source row exists.
    source_id: str = ""
    title: str = ""
    creator: str = ""
    description: str = ""
    published_at: datetime | None = None
    language: str = "en"
    topics: tuple[str, ...] = ()
    #: The platform's own id — a YouTube video id, a feed item GUID. What makes
    #: the same item recognisable across a re-crawl.
    external_id: str = ""
    media_url: str = ""
    download: Download | None = None
    probe: MediaProbe | None = None
    thumbnail: Thumbnail | None = None
    #: Everything the adapter reported and this layer has no column for. Kept
    #: whole, because a field nobody needed yet is the one the next feature
    #: needs and the crawl is expensive to repeat.
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def complete(self) -> bool:
        return (
            self.download is not None
            and self.download.state is DownloadState.COMPLETE
            and self.probe is not None
            and self.probe.usable
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "acquisition_id": self.acquisition_id,
            "kind": self.ref.kind.value,
            "key": self.ref.key,
            "source_id": self.source_id,
            "title": self.title,
            "creator": self.creator,
            "external_id": self.external_id,
            "complete": self.complete,
            "download": self.download.to_dict() if self.download else None,
            "probe": self.probe.to_dict() if self.probe else None,
            "thumbnail": self.thumbnail.to_dict() if self.thumbnail else None,
            "error": self.error,
        }
