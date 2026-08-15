"""What rendering works with: a job, a result, and the errors between.

The gameplay engine already decides *what* the frame looks like — panels,
camera path, timing, caption zone — and `gameplay.render` already turns that
into an ffmpeg filtergraph. Nothing executed it. This package does.

Two rules the layer holds to:

* **A render is not finished until it has been measured.** ffmpeg exits zero on
  plenty of files that are not what was asked for: a truncated encode, a video
  with the audio silently dropped because a map failed, a 1080x1922 output
  because a scale rounded odd. The output is probed and checked against the
  plan before it counts.
* **A partial file is never the output file.** ffmpeg writes to a temporary
  path and the result is renamed into place, so a killed worker leaves nothing
  a later stage can mistake for a finished render.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..acquire.types import MediaProbe
from ..publish.types import utcnow

__all__ = [
    "RenderState",
    "RenderRequest",
    "RenderResult",
    "RenderError",
    "RenderFailed",
    "OutputRejected",
    "FfmpegMissing",
]


class RenderState(str, enum.Enum):
    PENDING = "pending"
    RENDERING = "rendering"
    READY = "ready"
    FAILED = "failed"


class RenderError(Exception):
    """Base for everything this package raises."""


class FfmpegMissing(RenderError):
    """No ffmpeg. Named plainly rather than surfacing as a mystery.

    There is no fallback worth having: this stage is ffmpeg. A renderer that
    degraded to "no video" would let a pipeline report success and publish
    nothing.
    """


class RenderFailed(RenderError):
    """ffmpeg refused, timed out, or died."""


class OutputRejected(RenderError):
    """ffmpeg exited zero and produced something wrong.

    The interesting failure. A truncated encode, a missing audio track, the
    wrong geometry — all of which pass a "did the process succeed?" check and
    none of which are publishable.
    """


@dataclass(slots=True)
class RenderRequest:
    """One clip to render."""

    render_id: str
    #: The composition, from `gameplay.compose`.
    plan: Any
    #: The speaker's media — what acquisition downloaded.
    speaker_path: str
    #: Where the finished file lands.
    output_path: str
    gameplay_path: str = ""
    #: ASS subtitles to burn in. Burned rather than muxed: TikTok, Reels and
    #: Shorts all play with soft subtitles off by default, so a soft track is
    #: a caption nobody sees.
    subtitles_path: str = ""
    #: Trim the speaker's media to the clip. Without this the renderer encodes
    #: the whole two-hour podcast and takes the first thirty seconds of it.
    start_s: float = 0.0
    clip_id: str = ""
    source_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "render_id": self.render_id,
            "speaker_path": self.speaker_path,
            "gameplay_path": self.gameplay_path,
            "subtitles_path": self.subtitles_path,
            "output_path": self.output_path,
            "start_s": self.start_s,
            "clip_id": self.clip_id,
        }


@dataclass(slots=True)
class RenderResult:
    render_id: str
    state: RenderState = RenderState.PENDING
    output_path: str = ""
    #: What the output turned out to be, measured rather than assumed.
    probe: MediaProbe | None = None
    checksum: str = ""
    size_bytes: int = 0
    #: Wall-clock, and the ratio against the clip's own length. A render
    #: slower than realtime is the number that decides how many workers 500
    #: uploads a day needs.
    elapsed_s: float = 0.0
    realtime_ratio: float = 0.0
    attempts: int = 0
    error: str = ""
    video_id: str = ""
    #: Where the finished clip durably lives, as `r2://bucket/key`. Empty when
    #: no storage is configured, in which case `output_path` on whichever
    #: container ran the render is the only copy.
    storage_ref: str = ""
    #: The unsigned URL Instagram fetches from. Empty unless the object is
    #: really there and the bucket really has a public domain — a fabricated
    #: one fails at Meta's fetcher with an error naming neither.
    public_url: str = ""
    finished_at: datetime | None = field(default=None)

    @property
    def ok(self) -> bool:
        return self.state is RenderState.READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "render_id": self.render_id,
            "state": self.state.value,
            "output_path": self.output_path,
            "size_bytes": self.size_bytes,
            "elapsed_s": round(self.elapsed_s, 2),
            "realtime_ratio": round(self.realtime_ratio, 2),
            "probe": self.probe.to_dict() if self.probe else None,
            "video_id": self.video_id,
            "error": self.error,
        }
