"""ClipForge AI — stream clipper.

Finds clippable moments in recorded Twitch, Kick, and YouTube Live streams and
cuts them into vertical 15/30/45/60-second clips.

    from clipforge.stream import Platform, StreamClipperEngine, build_session

    session = build_session(
        session_id="vod-123",
        platform=Platform.TWITCH,
        duration_ms=4 * 3600 * 1000,
        raw_chat=twitch_comments,
        raw_events=twitch_events,
    )
    for clip in StreamClipperEngine().clip(session).clips:
        print(clip.scores.virality, clip.duration_s, clip.title)

Chat is the primary signal: a thousand viewers reacting in real time is a
better moment detector than any model running on the audio. The one thing that
has to be right is timing — chat lags the stream by several seconds, so the
clip has to start *before* the spike. See `anchors.py`.
"""

from .adapters import (
    build_session,
    kick_chat,
    kick_events,
    twitch_chat,
    twitch_events,
    youtube_chat,
    youtube_events,
)
from .engine import ClipperConfig, StreamClipperEngine
from .layout import Destination, LayoutStyle, SAFE_ZONES
from .types import (
    Anchor,
    ChatMessage,
    ClipperResult,
    CLIP_DURATIONS_S,
    EventKind,
    Platform,
    Scores,
    Spike,
    StreamClip,
    StreamEvent,
    StreamSession,
    StreamSignal,
    VerticalLayout,
    VideoRegion,
)

__all__ = [
    "Anchor",
    "CLIP_DURATIONS_S",
    "ChatMessage",
    "ClipperConfig",
    "ClipperResult",
    "Destination",
    "EventKind",
    "LayoutStyle",
    "Platform",
    "SAFE_ZONES",
    "Scores",
    "Spike",
    "StreamClip",
    "StreamClipperEngine",
    "StreamEvent",
    "StreamSession",
    "StreamSignal",
    "VerticalLayout",
    "VideoRegion",
    "build_session",
    "kick_chat",
    "kick_events",
    "twitch_chat",
    "twitch_events",
    "youtube_chat",
    "youtube_events",
]
