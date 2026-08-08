"""Core types for the stream clipper.

A stream session is three parallel timelines — chat, platform events, and
(optionally) a transcript. The clipper fuses them; everything here is the
vocabulary for that fusion.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Sequence


class Platform(str, enum.Enum):
    TWITCH = "twitch"
    KICK = "kick"
    YOUTUBE_LIVE = "youtube_live"


class StreamSignal(str, enum.Enum):
    """The eight moment categories the stream clipper detects.

    Stable wire identifiers — persisted on the moment record, so do not rename.
    """

    RAGE = "rage"
    FUNNY = "funny"
    WIN = "win"
    FAIL = "fail"
    REACTION = "reaction"
    DONATION = "donation"
    ARGUMENT = "argument"
    EMOTIONAL = "emotional"


class EventKind(str, enum.Enum):
    """Platform events that carry money or status, normalised across sources."""

    DONATION = "donation"          # Twitch bits / Kick tips / YouTube Super Chat
    SUBSCRIPTION = "subscription"  # subs, memberships
    GIFT = "gift"                  # gifted subs / gifted memberships
    RAID = "raid"                  # incoming raid or host
    FOLLOW = "follow"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One chat message, normalised from any platform.

    `offset_ms` is relative to stream start, not wall clock — every platform
    exports timestamps differently and the adapters resolve that.
    """

    offset_ms: int
    author: str
    text: str
    emotes: tuple[str, ...] = ()
    is_moderator: bool = False
    is_subscriber: bool = False


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """A platform event with an optional monetary amount."""

    offset_ms: int
    kind: EventKind
    author: str
    amount: float = 0.0
    currency: str = "USD"
    message: str = ""
    tier: str = ""

    @property
    def is_monetary(self) -> bool:
        return self.kind in (EventKind.DONATION, EventKind.SUBSCRIPTION, EventKind.GIFT)


@dataclass(frozen=True, slots=True)
class VideoRegion:
    """A named rectangle in the source frame, in source pixel coordinates.

    Supplied by the caller from the stream's scene layout (OBS scene JSON, a
    detected facecam bounding box, or a manual annotation). The layout planner
    needs to know where the facecam and gameplay live to build a vertical
    composition that is not just a centre crop of a 16:9 frame.
    """

    name: str
    x: int
    y: int
    width: int
    height: int

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0


@dataclass(frozen=True, slots=True)
class StreamSession:
    """A complete recorded stream: chat, events, layout, optional transcript."""

    session_id: str
    platform: Platform
    duration_ms: int
    chat: tuple[ChatMessage, ...] = ()
    events: tuple[StreamEvent, ...] = ()
    regions: tuple[VideoRegion, ...] = ()
    source_width: int = 1920
    source_height: int = 1080
    transcript: Any = None  # clipforge.viral.Transcript, kept loose to avoid a hard dep

    def region(self, name: str) -> VideoRegion | None:
        return next((r for r in self.regions if r.name == name), None)


@dataclass(frozen=True, slots=True)
class SignalSample:
    """Evidence for one signal at one instant, from one source of truth."""

    offset_ms: int
    signal: StreamSignal
    strength: float
    origin: str  # "chat" | "event" | "transcript"
    evidence: str = ""


@dataclass(frozen=True, slots=True)
class Spike:
    """A burst of chat activity above the rolling baseline.

    `onset_ms` is when the burst *started*, which is the number that matters:
    the on-stream moment precedes it by the reaction lag. `peak_ms` is where
    chat was loudest and is almost always too late to start a clip.
    """

    onset_ms: int
    peak_ms: int
    end_ms: int
    peak_rate: float
    baseline_rate: float

    @property
    def magnitude(self) -> float:
        """How many times above baseline the peak reached."""
        return self.peak_rate / self.baseline_rate if self.baseline_rate > 0 else 0.0


@dataclass(frozen=True, slots=True)
class Anchor:
    """A moment on the stream timeline worth clipping around.

    `offset_ms` is the estimated instant the thing *happened on screen*, which
    is not the same as when chat reacted to it — see `anchors.py`.
    """

    offset_ms: int
    signals: dict[StreamSignal, float]
    intensity: float
    spike: Spike | None = None
    evidence: tuple[str, ...] = ()

    @property
    def dominant(self) -> StreamSignal | None:
        if not self.signals:
            return None
        return max(self.signals, key=lambda s: self.signals[s])


# The four durations the product ships. Fixed rather than continuous because
# editors and schedulers reason in these units, and because each length wants a
# different amount of setup before the moment lands.
CLIP_DURATIONS_S: tuple[int, ...] = (15, 30, 45, 60)


@dataclass(frozen=True, slots=True)
class Scores:
    """Per-clip scores, 0-100."""

    virality: int
    hype: int
    retention: int
    clarity: int

    def as_dict(self) -> dict[str, int]:
        return {
            "virality": self.virality,
            "hype": self.hype,
            "retention": self.retention,
            "clarity": self.clarity,
        }


@dataclass(frozen=True, slots=True)
class Crop:
    """A source-space rectangle mapped to a destination rectangle in the frame."""

    source: VideoRegion
    dest_x: int
    dest_y: int
    dest_width: int
    dest_height: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": {
                "name": self.source.name,
                "x": self.source.x,
                "y": self.source.y,
                "width": self.source.width,
                "height": self.source.height,
            },
            "dest": {
                "x": self.dest_x,
                "y": self.dest_y,
                "width": self.dest_width,
                "height": self.dest_height,
            },
        }


@dataclass(frozen=True, slots=True)
class VerticalLayout:
    """A declarative 9:16 composition plan.

    This is a render spec, not pixels — deterministic and hashable, so the
    render layer can cache on it exactly as the architecture's edit decision
    list does.
    """

    name: str
    width: int
    height: int
    crops: tuple[Crop, ...]
    background: str = "blurred_source"
    caption_zone: tuple[int, int, int, int] = (0, 0, 0, 0)  # x, y, w, h
    chat_overlay: tuple[int, int, int, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "background": self.background,
            "crops": [c.to_dict() for c in self.crops],
            "caption_zone": {
                "x": self.caption_zone[0],
                "y": self.caption_zone[1],
                "width": self.caption_zone[2],
                "height": self.caption_zone[3],
            },
            "chat_overlay": (
                {
                    "x": self.chat_overlay[0],
                    "y": self.chat_overlay[1],
                    "width": self.chat_overlay[2],
                    "height": self.chat_overlay[3],
                }
                if self.chat_overlay
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class StreamClip:
    """One rendered-ready clip variant."""

    session_id: str
    platform: Platform
    start_ms: int
    end_ms: int
    duration_s: int
    anchor: Anchor
    scores: Scores
    layout: VerticalLayout
    title: str
    signals: dict[StreamSignal, float] = field(default_factory=dict)
    features: dict[str, float] = field(default_factory=dict)

    @property
    def anchor_position(self) -> float:
        """Where the moment sits within the clip, 0.0 = start, 1.0 = end."""
        span = self.end_ms - self.start_ms
        return (self.anchor.offset_ms - self.start_ms) / span if span else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "platform": self.platform.value,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_s": self.duration_s,
            "title": self.title,
            "anchor_ms": self.anchor.offset_ms,
            "anchor_position": round(self.anchor_position, 3),
            "scores": self.scores.as_dict(),
            "signals": {
                s.value: round(v, 4)
                for s, v in sorted(self.signals.items(), key=lambda kv: -kv[1])
                if v > 0
            },
            "features": {k: round(v, 4) for k, v in sorted(self.features.items())},
            "evidence": list(self.anchor.evidence),
            "layout": self.layout.to_dict(),
        }


@dataclass(slots=True)
class ClipperResult:
    session_id: str
    clips: list[StreamClip]
    anchors: list[Anchor]
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "stats": self.stats,
            "clips": [c.to_dict() for c in self.clips],
        }


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def saturating_sum(values: Sequence[float]) -> float:
    """Probabilistic OR — combine independent evidence without exceeding 1."""
    remaining = 1.0
    for v in values:
        remaining *= 1.0 - clamp(v)
    return 1.0 - remaining
